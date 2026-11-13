"""Apply detection of resource deletions (roadmap H6): the resource is absent."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization.applied_detector import (
    AppliedDetector,
    K8sResourceReader,
    ResourceReadError,
)
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.gitops.contracts import ActionStatus, PlanStatus, ResourceTarget
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

PV = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-orphan")


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


class _NoWorkloads:
    async def read(self):
        return {}


class _Resources:
    def __init__(self, live: dict[tuple[str, str, str], Any] | Exception) -> None:
        self.live = live
        self.reads: list[ResourceTarget] = []

    async def read_resource(self, ref: ResourceTarget):
        self.reads.append(ref)
        if isinstance(self.live, Exception):
            raise self.live
        return self.live.get((ref.kind, ref.namespace, ref.name))


class _Plan:
    plan_id = "pln-delete"
    digest = None
    rollback_of_plan_id = None
    pre_apply_baseline = None
    action = None

    def __init__(self, recommendation_id: int) -> None:
        self.recommendation_ids = [recommendation_id]
        self.created_at = datetime.now(timezone.utc)
        self.status = PlanStatus.SYNCED


class _Actions:
    def __init__(self, recommendation_id: int, status: ActionStatus | None) -> None:
        self.status = status
        self.plan = _Plan(recommendation_id)
        self.updates: list[ActionStatus] = []

    async def get_recommendation_action_status(self, recommendation_id: int):
        return self.status

    async def set_recommendation_action_status(self, ids, status, *, expected_statuses=None, actor="", plan_id=None):
        self.updates.append(status)
        self.status = status
        return len(list(ids))

    async def list_plans_for_recommendation(self, recommendation_id: int) -> Sequence[Any]:
        return [self.plan]

    async def set_plan_status(self, plan_id, status, **kwargs):
        self.plan.status = status
        return self.plan


class _Recorder:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, list[int], Any, Any]] = []

    async def needs_observations(self) -> bool:
        return False

    async def observe_workloads(self, snapshots, now=None) -> int:
        return 0

    async def change_id_for(self, plan_id, snapshot, completed_at):
        return f"chg-{snapshot.kind}-{snapshot.name}"

    async def record_change(self, change_id, records, *, snapshot=None, plan=None, now=None):
        self.recorded.append((change_id, [record.id for record in records], snapshot, plan))


def _pv_record() -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="pv-orphan",
        type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        description="PersistentVolume 'pv-orphan' appears unused",
        scope="cluster",
        potential_savings_cost=120.0,
    )


async def _saved(repo) -> RecommendationRecord:
    await repo.save_recommendations([_pv_record()])
    (record,) = await repo.get_recommendations_by_statuses(["active"])
    assert record.id is not None
    return record


@pytest.mark.asyncio
async def test_a_deletion_merged_through_a_plan_is_applied_once_the_resource_is_absent(repo) -> None:
    record = await _saved(repo)
    assert record.id is not None
    actions = _Actions(record.id, ActionStatus.SYNC_PENDING)
    recorder = _Recorder()
    detector = AppliedDetector(
        RecommendationLifecycle(repo),
        reader=_NoWorkloads(),
        config=Config(),
        action_status_repository=cast(Any, actions),
        change_recorder=recorder,
        resource_reader=_Resources({}),
    )

    (applied,) = await detector.detect()

    assert applied.status == RecommendationStatus.APPLIED
    assert applied.application_method == "pr_merge"
    assert actions.updates == [ActionStatus.APPLIED]
    ((change_id, ids, snapshot, plan),) = recorder.recorded
    assert change_id == "chg-PersistentVolume-pv-orphan" and ids == [record.id]
    assert (snapshot.namespace, snapshot.kind, snapshot.name, snapshot.action_kind) == (
        "",
        "PersistentVolume",
        "pv-orphan",
        "delete_resource",
    )
    assert plan is actions.plan


@pytest.mark.asyncio
async def test_a_resource_deleted_by_hand_is_detected_and_measured(repo) -> None:
    record = await _saved(repo)
    assert record.id is not None
    recorder = _Recorder()
    detector = AppliedDetector(
        RecommendationLifecycle(repo),
        reader=_NoWorkloads(),
        config=Config(),
        action_status_repository=cast(Any, _Actions)(record.id, None),
        change_recorder=recorder,
        resource_reader=_Resources({}),
    )

    (applied,) = await detector.detect()

    assert applied.application_method == "detected"
    assert recorder.recorded and recorder.recorded[0][3] is None


@pytest.mark.asyncio
async def test_a_present_or_unreadable_resource_is_never_applied(repo) -> None:
    await _saved(repo)
    present = _Resources({("PersistentVolume", "", "pv-orphan"): {"metadata": {"name": "pv-orphan"}}})
    detector = AppliedDetector(
        RecommendationLifecycle(repo), reader=_NoWorkloads(), config=Config(), resource_reader=present
    )
    assert await detector.detect() == []
    assert present.reads == [PV]

    unreadable = AppliedDetector(
        RecommendationLifecycle(repo),
        reader=_NoWorkloads(),
        config=Config(),
        resource_reader=_Resources(ResourceReadError("forbidden")),
    )
    assert await unreadable.detect() == []


@pytest.mark.asyncio
async def test_an_open_pull_request_is_not_taken_for_a_manual_deletion(repo) -> None:
    record = await _saved(repo)
    assert record.id is not None
    detector = AppliedDetector(
        RecommendationLifecycle(repo),
        reader=_NoWorkloads(),
        config=Config(),
        action_status_repository=cast(Any, _Actions)(record.id, ActionStatus.PR_OPEN),
        resource_reader=_Resources({}),
    )
    assert await detector.detect() == []


class _CoreApi:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def read_persistent_volume(self, **kwargs: Any) -> Any:
        self.calls.append(("read_persistent_volume", kwargs))
        if self.error is not None:
            raise self.error
        return {"metadata": {"name": kwargs["name"]}}

    async def read_namespaced_service(self, **kwargs: Any) -> Any:
        self.calls.append(("read_namespaced_service", kwargs))
        return {"metadata": {"name": kwargs["name"]}}


class _ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


@pytest.mark.asyncio
async def test_the_kubernetes_reader_maps_only_404_to_absent() -> None:
    assert await K8sResourceReader(_CoreApi()).read_resource(PV) == {"metadata": {"name": "pv-orphan"}}
    assert await K8sResourceReader(_CoreApi(_ApiError(404))).read_resource(PV) is None
    with pytest.raises(ResourceReadError):
        await K8sResourceReader(_CoreApi(_ApiError(403))).read_resource(PV)
    service = ResourceTarget(api_version="v1", kind="Service", namespace="shop", name="lb")
    api = _CoreApi()
    await K8sResourceReader(api).read_resource(service)
    assert api.calls == [("read_namespaced_service", {"name": "lb", "namespace": "shop"})]
    with pytest.raises(ResourceReadError, match="not supported"):
        await K8sResourceReader(api).read_resource(
            ResourceTarget(api_version="v1", kind="ConfigMap", namespace="a", name="b")
        )
