# tests/core/optimization/test_applied_detector.py
"""Tests for Kubernetes-based apply detection."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization.applied_detector import (
    AppliedDetector,
    K8sWorkloadReader,
    SyncPlanStatus,
    WorkloadSnapshot,
    _sum_requests,
)
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    ContainerResources,
    PlanStatus,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
)
from greenkube.models.metrics import (
    RecommendationPatchV2,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


class FakeReader:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    async def read(self):
        return self._snapshots


def _cpu_record(
    current: int = 1000,
    recommended: int = 300,
    status: RecommendationStatus = RecommendationStatus.ACTIVE,
) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        status=status,
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=recommended,
    )


def _memory_record() -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="Memory oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_memory_request_bytes=1024 * 1024 * 1024,
        recommended_memory_request_bytes=256 * 1024 * 1024,
    )


def _plan_cpu_record() -> RecommendationRecord:
    record = _cpu_record(current=1000, recommended=300)
    action = ActionPlanV3(
        action_id="action-1",
        recommendation_ids=[1],
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="prod",
            name="api",
        ),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation:1",
    )
    return record.model_copy(
        update={
            "container_name": "api",
            "action": action,
            "patch": RecommendationPatchV2(action=action),
        }
    )


def _pre_apply_baseline(
    *,
    generation: int,
    cpu_millicores: int,
    uid: str = "uid-api-1",
    memory_bytes: int | None = None,
) -> PreApplyWorkloadBaseline:
    return PreApplyWorkloadBaseline(
        workload_uid=uid,
        generation=generation,
        observed_generation=generation,
        container_requests={
            "api": {
                "cpu_millicores": cpu_millicores,
                "memory_bytes": memory_bytes,
            }
        },
    )


class FakeActionStatusRepository:
    def __init__(
        self,
        status: ActionStatus,
        *,
        pre_apply_baseline: PreApplyWorkloadBaseline | None = None,
    ) -> None:
        self.status = status
        self.plan = FakeSyncPlan(
            1,
            PlanStatus.MERGED,
            action=_plan_cpu_record().action,
            pre_apply_baseline=pre_apply_baseline or _pre_apply_baseline(generation=10, cpu_millicores=1000),
        )
        self.updates: list[tuple[list[int], ActionStatus]] = []
        self.plan_updates: list[PlanStatus] = []

    @property
    def plan_status(self) -> PlanStatus:
        return self.plan.status

    @plan_status.setter
    def plan_status(self, value: PlanStatus) -> None:
        self.plan.status = value

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus:
        assert recommendation_id == 1
        return self.status

    async def set_recommendation_action_status(
        self,
        recommendation_ids,
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        ids = list(recommendation_ids)
        self.updates.append((ids, status))
        self.status = status
        return len(ids)

    async def list_plans_for_recommendation(self, recommendation_id: int) -> Sequence[SyncPlanStatus]:
        return [self.plan]

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> SyncPlanStatus | None:
        if self.plan.plan_id != plan_id:
            return None
        self.plan.status = status
        self.plan_updates.append(status)
        return self.plan


class FakeLegacyActionStatusRepository:
    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus:
        assert recommendation_id == 1
        return ActionStatus.PR_OPEN

    async def set_recommendation_action_status(
        self,
        recommendation_ids,
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        return len(list(recommendation_ids))

    async def list_plans_for_recommendation(self, recommendation_id: int) -> Sequence[SyncPlanStatus]:
        assert recommendation_id == 1
        return []

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> SyncPlanStatus | None:
        return None


class FakeSyncPlan:
    def __init__(
        self,
        recommendation_id: int,
        status: PlanStatus,
        *,
        action: ActionPlanV3 | None = None,
        rollback_of_plan_id: str | None = None,
        pre_apply_baseline: PreApplyWorkloadBaseline | None = None,
    ) -> None:
        self._recommendation_id = recommendation_id
        self._status = status
        self._created_at = datetime.now(timezone.utc)
        self._action = action or _plan_cpu_record().action
        self._rollback_of_plan_id = rollback_of_plan_id
        self._pre_apply_baseline = pre_apply_baseline

    @property
    def plan_id(self) -> str:
        return "plan-1"

    @property
    def digest(self) -> str | None:
        return None

    @property
    def recommendation_ids(self) -> list[int]:
        return [self._recommendation_id]

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def status(self) -> PlanStatus:
        return self._status

    @status.setter
    def status(self, value: PlanStatus) -> None:
        self._status = value

    @property
    def rollback_of_plan_id(self) -> str | None:
        return self._rollback_of_plan_id

    @property
    def action(self) -> ActionPlanV3:
        assert self._action is not None
        return self._action

    @property
    def pre_apply_baseline(self) -> PreApplyWorkloadBaseline | None:
        return self._pre_apply_baseline

    @pre_apply_baseline.setter
    def pre_apply_baseline(self, value: PreApplyWorkloadBaseline | None) -> None:
        self._pre_apply_baseline = value


@dataclass(slots=True)
class FakeRollbackSyncPlan:
    plan_id: str
    recommendation_ids: list[int]
    status: PlanStatus
    action: ActionPlanV3
    created_at: datetime
    rollback_of_plan_id: str | None = None
    pre_apply_baseline: PreApplyWorkloadBaseline | None = None
    renderer: RendererKind | None = None
    digest: str | None = None


class FakeRollbackActionStatusRepository:
    def __init__(self, recommendation_id: int, plans: list[FakeRollbackSyncPlan]) -> None:
        self.statuses = {recommendation_id: ActionStatus.SYNC_PENDING}
        self.plans = plans
        self.updates: list[tuple[list[int], ActionStatus]] = []
        self.plan_updates: list[PlanStatus] = []

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None:
        return self.statuses.get(recommendation_id)

    async def set_recommendation_action_status(
        self,
        recommendation_ids,
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        ids = list(recommendation_ids)
        self.updates.append((ids, status))
        for recommendation_id in ids:
            self.statuses[recommendation_id] = status
        return len(ids)

    async def list_plans_for_recommendation(self, recommendation_id: int) -> Sequence[SyncPlanStatus]:
        return [plan for plan in self.plans if recommendation_id in plan.recommendation_ids]

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> SyncPlanStatus | None:
        plan = next((item for item in self.plans if item.plan_id == plan_id), None)
        if plan is None:
            return None
        plan.status = status
        self.plan_updates.append(status)
        return plan


def _rollback_plan_set(
    record: RecommendationRecord,
    rollback_status: PlanStatus,
    *,
    renderer: RendererKind | None = None,
):
    assert record.id is not None and record.action is not None
    original = FakeRollbackSyncPlan(
        plan_id="original-plan",
        recommendation_ids=[record.id],
        status=PlanStatus.SYNCED,
        action=record.action,
        created_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        rollback_of_plan_id=None,
        pre_apply_baseline=_pre_apply_baseline(generation=10, cpu_millicores=1000),
        renderer=renderer,
    )
    rollback = FakeRollbackSyncPlan(
        plan_id="rollback-plan",
        recommendation_ids=[record.id],
        status=rollback_status,
        action=record.action,
        created_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        rollback_of_plan_id="original-plan",
        pre_apply_baseline=_pre_apply_baseline(generation=11, cpu_millicores=300),
        renderer=renderer,
    )
    return [original, rollback]


class TestAppliedDetector:
    @pytest.mark.asyncio
    async def test_kubernetes_reader_includes_workload_generation_and_container_requests(self, monkeypatch):
        import greenkube.core.k8s_client as k8s_client

        item = SimpleNamespace(
            metadata=SimpleNamespace(namespace="prod", name="api", uid="uid-api-1", generation=8),
            status=SimpleNamespace(observed_generation=8),
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    spec=SimpleNamespace(
                        containers=[
                            SimpleNamespace(
                                name="api",
                                resources=SimpleNamespace(requests={"cpu": "500m", "memory": "128Mi"}),
                            )
                        ]
                    )
                )
            ),
        )

        async def deployments():
            return SimpleNamespace(items=[item])

        async def empty_list():
            return SimpleNamespace(items=[])

        async def get_api():
            return SimpleNamespace(
                list_deployment_for_all_namespaces=deployments,
                list_stateful_set_for_all_namespaces=empty_list,
                list_daemon_set_for_all_namespaces=empty_list,
            )

        monkeypatch.setattr(k8s_client, "get_apps_v1_api", get_api)

        snapshots = await K8sWorkloadReader().read()
        snapshot = snapshots[("prod", "Deployment", "api")]

        assert snapshot.uid == "uid-api-1"
        assert snapshot.generation == 8
        assert snapshot.observed_generation == 8
        assert snapshot.container_request_values == {"api": (500, 128 * 1024 * 1024)}
        assert snapshot.container_request_values_valid

    def test_kubernetes_request_summary_preserves_each_container(self):
        containers = [
            SimpleNamespace(
                name="api",
                resources=SimpleNamespace(requests={"cpu": "500m", "memory": "128Mi"}),
            ),
            SimpleNamespace(
                name="sidecar",
                resources=SimpleNamespace(requests={"cpu": "200m", "memory": "64Mi"}),
            ),
        ]

        cpu, memory, per_container = _sum_requests(containers)

        assert cpu == 700
        assert memory == 192 * 1024 * 1024
        assert per_container == {
            "api": (500, 128 * 1024 * 1024),
            "sidecar": (200, 64 * 1024 * 1024),
        }

    @pytest.mark.asyncio
    async def test_detects_cpu_decrease(self, repo):
        await repo.save_recommendations([_cpu_record()])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 300, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].status == RecommendationStatus.APPLIED
        assert applied[0].application_method == "detected"
        assert applied[0].actual_cpu_request_millicores == 300

    @pytest.mark.asyncio
    async def test_ignores_unchanged_request(self, repo):
        await repo.save_recommendations([_cpu_record()])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 1000, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert await detector.detect() == []

    @pytest.mark.asyncio
    async def test_decrease_not_matching_recommendation_is_not_applied(self, repo):
        await repo.save_recommendations([_cpu_record(current=1000, recommended=300)])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 600, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert await detector.detect() == []

    @pytest.mark.asyncio
    async def test_within_tolerance_of_recommendation(self, repo):
        await repo.save_recommendations([_cpu_record(current=1000, recommended=300)])
        # 320m is within 25% of the 300m target even though the drop is < 25%.
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 320, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert len(await detector.detect()) == 1

    @pytest.mark.asyncio
    async def test_multi_container_aggregate_drop_does_not_apply_untargeted_recommendation(self, repo):
        record = _cpu_record(current=1000, recommended=300)
        await repo.save_recommendations([record])
        snapshot = WorkloadSnapshot(
            "prod",
            "Deployment",
            "api",
            700,
            0,
            container_requests={"api": (500, 0), "sidecar": (200, 0)},
        )
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader({("prod", "Deployment", "api"): snapshot}),
            config=Config(),
        )

        assert await detector.detect() == []

    @pytest.mark.asyncio
    async def test_named_container_applies_when_its_own_request_matches(self, repo):
        record = _cpu_record(current=1000, recommended=300).model_copy(update={"container_name": "api"})
        await repo.save_recommendations([record])
        snapshot = WorkloadSnapshot(
            "prod",
            "Deployment",
            "api",
            500,
            0,
            container_requests={"api": (300, 0), "sidecar": (200, 0)},
        )
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader({("prod", "Deployment", "api"): snapshot}),
            config=Config(),
        )

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].actual_cpu_request_millicores == 300

    @pytest.mark.asyncio
    async def test_typed_gitops_action_waits_for_sync_before_apply_detection(self, repo):
        await repo.save_recommendations([_plan_cpu_record()])
        action_repository = FakeActionStatusRepository(ActionStatus.MERGED)
        snapshot = WorkloadSnapshot(
            "prod",
            "Deployment",
            "api",
            500,
            0,
            container_requests={"api": (300, 0), "sidecar": (200, 0)},
            uid="uid-api-1",
            generation=11,
            observed_generation=11,
        )
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader({("prod", "Deployment", "api"): snapshot}),
            config=Config(),
            action_status_repository=action_repository,
        )

        assert await detector.detect() == []
        assert action_repository.updates == []

        action_repository.status = ActionStatus.SYNC_PENDING
        assert await detector.detect() == []

        action_repository.plan_status = PlanStatus.SYNCED
        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].application_method == "pr_merge"
        assert action_repository.status is ActionStatus.APPLIED
        assert action_repository.updates == [([1], ActionStatus.APPLIED)]
        assert action_repository.plan_status is PlanStatus.APPLIED
        assert action_repository.plan_updates == [PlanStatus.APPLIED]

    @pytest.mark.asyncio
    async def test_typed_gitops_action_without_pre_apply_baseline_remains_pending(self, repo):
        await repo.save_recommendations([_plan_cpu_record()])
        action_repository = FakeActionStatusRepository(ActionStatus.SYNC_PENDING)
        action_repository.plan.pre_apply_baseline = None
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        300,
                        0,
                        container_requests={"api": (300, 0)},
                        uid="uid-api-1",
                        generation=11,
                        observed_generation=11,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        assert await detector.detect() == []
        assert action_repository.status is ActionStatus.SYNC_PENDING
        assert action_repository.updates == []

    @pytest.mark.asyncio
    async def test_typed_plan_without_recommendation_action_cannot_fall_through_to_legacy_detection(self, repo):
        missing_action = _plan_cpu_record().model_copy(update={"action": None, "patch": None})
        await repo.save_recommendations([missing_action])
        action_repository = FakeActionStatusRepository(ActionStatus.PR_OPEN)
        action_repository.plan.status = PlanStatus.SYNCED
        action_repository.plan.pre_apply_baseline = None
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        300,
                        0,
                        container_requests={"api": (300, 0)},
                        uid="uid-api-1",
                        generation=11,
                        observed_generation=11,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        assert await detector.detect() == []
        assert action_repository.updates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("uid", "generation", "observed_generation"),
        [
            ("uid-api-1", 10, 10),
            ("uid-api-1", 11, 10),
            ("uid-api-1", 0, 0),
        ],
        ids=["generation-unchanged", "generation-unobserved", "generation-missing"],
    )
    async def test_typed_gitops_action_requires_same_workload_and_observed_generation_advance(
        self,
        repo,
        uid,
        generation,
        observed_generation,
    ):
        await repo.save_recommendations([_plan_cpu_record()])
        action_repository = FakeActionStatusRepository(ActionStatus.SYNC_PENDING)
        action_repository.plan.status = PlanStatus.SYNCED
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        300,
                        0,
                        container_requests={"api": (300, 0)},
                        uid=uid,
                        generation=generation,
                        observed_generation=observed_generation,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        assert await detector.detect() == []
        assert action_repository.status is ActionStatus.SYNC_PENDING
        assert action_repository.updates == []

    @pytest.mark.asyncio
    async def test_typed_gitops_action_accepts_recreated_workload_after_generation_is_observed(self, repo):
        await repo.save_recommendations([_plan_cpu_record()])
        action_repository = FakeActionStatusRepository(ActionStatus.SYNC_PENDING)
        action_repository.plan.status = PlanStatus.SYNCED
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        300,
                        0,
                        container_requests={"api": (300, 0)},
                        uid="uid-api-recreated",
                        generation=1,
                        observed_generation=1,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        applied = await detector.detect()

        assert [item.status for item in applied] == [RecommendationStatus.APPLIED]
        assert action_repository.status is ActionStatus.APPLIED

    @pytest.mark.asyncio
    async def test_rollback_reverts_only_after_synced_plan_and_preapply_container_requests_match(self, repo):
        await repo.save_recommendations(
            [_plan_cpu_record().model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})]
        )
        record = (await repo.get_recommendations_by_statuses(["rollback_review"]))[0]
        assert record.id is not None
        action_repository = FakeRollbackActionStatusRepository(record.id, _rollback_plan_set(record, PlanStatus.SYNCED))
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        1000,
                        0,
                        container_requests={"api": (1000, 0)},
                        uid="uid-api-1",
                        generation=12,
                        observed_generation=12,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        reverted = await detector.detect()

        assert [item.status for item in reverted] == [RecommendationStatus.REVERTED]
        assert action_repository.statuses[record.id] is ActionStatus.REVERTED
        assert action_repository.updates == [([record.id], ActionStatus.REVERTED)]
        assert action_repository.plans[1].status is PlanStatus.APPLIED
        assert action_repository.plan_updates == [PlanStatus.APPLIED]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("live_memory_bytes", "expected_status"),
        [
            (512 * 1024 * 1024, RecommendationStatus.REVERTED),
            (256 * 1024 * 1024, RecommendationStatus.ROLLBACK_REVIEW),
        ],
        ids=["exact-preapply-memory", "memory-drift"],
    )
    async def test_flux_helmrelease_rollback_requires_exact_preapply_per_container_baseline(
        self,
        repo,
        live_memory_bytes,
        expected_status,
    ):
        await repo.save_recommendations(
            [_plan_cpu_record().model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})]
        )
        record = (await repo.get_recommendations_by_statuses(["rollback_review"]))[0]
        assert record.id is not None
        plans = _rollback_plan_set(record, PlanStatus.SYNCED, renderer=RendererKind.HELM_RELEASE)
        plans[0].pre_apply_baseline = _pre_apply_baseline(
            generation=10,
            cpu_millicores=1000,
            memory_bytes=512 * 1024 * 1024,
        )
        plans[1].pre_apply_baseline = _pre_apply_baseline(
            generation=11,
            cpu_millicores=300,
            memory_bytes=256 * 1024 * 1024,
        )
        action_repository = FakeRollbackActionStatusRepository(record.id, plans)
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        1000,
                        512 * 1024 * 1024,
                        container_requests={"api": (1000, live_memory_bytes)},
                        uid="uid-api-1",
                        generation=12,
                        observed_generation=12,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        reverted = await detector.detect()

        current = await repo.get_recommendation_by_id(record.id)
        assert current is not None
        assert current.status is expected_status
        if expected_status is RecommendationStatus.REVERTED:
            assert [item.status for item in reverted] == [RecommendationStatus.REVERTED]
            assert action_repository.statuses[record.id] is ActionStatus.REVERTED
            assert action_repository.plans[1].status is PlanStatus.APPLIED
        else:
            assert reverted == []
            assert action_repository.statuses[record.id] is ActionStatus.SYNC_PENDING
            assert action_repository.plans[1].status is PlanStatus.SYNCED

    @pytest.mark.asyncio
    async def test_rollback_plan_is_never_treated_as_a_forward_apply(self, repo):
        await repo.save_recommendations([_plan_cpu_record()])
        record = (await repo.get_active_recommendations())[0]
        assert record.id is not None
        action_repository = FakeRollbackActionStatusRepository(
            record.id,
            _rollback_plan_set(record, PlanStatus.SYNCED),
        )
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        300,
                        0,
                        container_requests={"api": (300, 0)},
                        uid="uid-api-1",
                        generation=12,
                        observed_generation=12,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        assert await detector.detect() == []
        current = await repo.get_recommendation_by_id(record.id)
        assert current is not None
        assert current.status is RecommendationStatus.ACTIVE
        assert action_repository.statuses[record.id] is ActionStatus.SYNC_PENDING
        assert action_repository.updates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("rollback_status", "container_requests"),
        [
            (PlanStatus.MERGED, {"api": (1000, 0)}),
            (PlanStatus.SYNCED, {"api": (900, 0)}),
            (PlanStatus.SYNCED, {}),
        ],
        ids=["merge-is-not-sync", "preapply-value-mismatch", "container-missing"],
    )
    async def test_rollback_remains_in_review_until_sync_and_exact_container_match(
        self,
        repo,
        rollback_status,
        container_requests,
    ):
        await repo.save_recommendations(
            [_plan_cpu_record().model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})]
        )
        record = (await repo.get_recommendations_by_statuses(["rollback_review"]))[0]
        assert record.id is not None
        action_repository = FakeRollbackActionStatusRepository(
            record.id,
            _rollback_plan_set(record, rollback_status),
        )
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        1000,
                        0,
                        container_requests=container_requests,
                        uid="uid-api-1",
                        generation=12,
                        observed_generation=12,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        assert await detector.detect() == []
        current = await repo.get_recommendation_by_id(record.id)
        assert current is not None
        assert current.status is RecommendationStatus.ROLLBACK_REVIEW
        assert action_repository.statuses[record.id] is ActionStatus.SYNC_PENDING
        assert action_repository.updates == []
        assert action_repository.plans[1].status is rollback_status
        assert action_repository.plan_updates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("generation", "observed_generation"),
        [(11, 11), (12, 11)],
        ids=["generation-unchanged", "generation-unobserved"],
    )
    async def test_rollback_requires_observed_generation_advance_after_rollback_baseline(
        self,
        repo,
        generation,
        observed_generation,
    ):
        await repo.save_recommendations(
            [_plan_cpu_record().model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})]
        )
        record = (await repo.get_recommendations_by_statuses(["rollback_review"]))[0]
        assert record.id is not None
        action_repository = FakeRollbackActionStatusRepository(record.id, _rollback_plan_set(record, PlanStatus.SYNCED))
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        1000,
                        0,
                        container_requests={"api": (1000, 0)},
                        uid="uid-api-1",
                        generation=generation,
                        observed_generation=observed_generation,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        assert await detector.detect() == []
        assert action_repository.statuses[record.id] is ActionStatus.SYNC_PENDING
        assert action_repository.updates == []
        assert action_repository.plans[1].status is PlanStatus.SYNCED

    @pytest.mark.asyncio
    async def test_rollback_accepts_recreated_workload_with_observed_preapply_requests(self, repo):
        await repo.save_recommendations(
            [_plan_cpu_record().model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})]
        )
        record = (await repo.get_recommendations_by_statuses(["rollback_review"]))[0]
        assert record.id is not None
        action_repository = FakeRollbackActionStatusRepository(record.id, _rollback_plan_set(record, PlanStatus.SYNCED))
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader(
                {
                    ("prod", "Deployment", "api"): WorkloadSnapshot(
                        "prod",
                        "Deployment",
                        "api",
                        1000,
                        0,
                        container_requests={"api": (1000, 0)},
                        uid="uid-api-recreated",
                        generation=1,
                        observed_generation=1,
                    )
                }
            ),
            config=Config(),
            action_status_repository=action_repository,
        )

        reverted = await detector.detect()

        assert [item.status for item in reverted] == [RecommendationStatus.REVERTED]
        assert action_repository.statuses[record.id] is ActionStatus.REVERTED

    @pytest.mark.asyncio
    async def test_pr_open_becomes_pr_merge(self, repo):
        await repo.save_recommendations([_cpu_record(status=RecommendationStatus.PR_OPEN)])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 300, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].application_method == "pr_merge"

    @pytest.mark.asyncio
    async def test_legacy_pr_open_with_backfilled_action_status_keeps_legacy_apply_detection(self, repo):
        await repo.save_recommendations([_cpu_record(status=RecommendationStatus.PR_OPEN)])
        detector = AppliedDetector(
            RecommendationLifecycle(repo),
            reader=FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 300, 0)}),
            config=Config(),
            action_status_repository=FakeLegacyActionStatusRepository(),
        )

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].application_method == "pr_merge"

    @pytest.mark.asyncio
    async def test_detects_memory_decrease(self, repo):
        await repo.save_recommendations([_memory_record()])
        reader = FakeReader(
            {("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 0, 256 * 1024 * 1024)}
        )
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].type == RecommendationType.RIGHTSIZING_MEMORY

    @pytest.mark.asyncio
    async def test_missing_snapshot_is_skipped(self, repo):
        await repo.save_recommendations([_cpu_record()])
        reader = FakeReader({})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert await detector.detect() == []

    @pytest.mark.asyncio
    async def test_zero_observed_request_is_not_a_decrease(self, repo):
        """A workload without requests must not be treated as an applied change."""
        await repo.save_recommendations([_cpu_record()])
        reader = FakeReader({("prod", "Deployment", "api"): WorkloadSnapshot("prod", "Deployment", "api", 0, 0)})
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=reader, config=Config())

        assert await detector.detect() == []
