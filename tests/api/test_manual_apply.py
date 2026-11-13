"""Manual mode (roadmap H9): kubectl snippets and "Mark as applied", measured like a PR."""

from __future__ import annotations

import json
import shlex
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_recommendation_repository
from greenkube.api.routers.manual_apply import get_manual_resource_reader, manual_guide
from greenkube.core.db import db_manager
from greenkube.core.optimization.applied_detector import ResourceReadError
from greenkube.gitops.contracts import ActionPlanV3, ContainerResources, ResourceTarget, SetContainerResources
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

EXPIRES = datetime.now(timezone.utc) + timedelta(days=14)


def _rightsizing(**overrides) -> RecommendationRecord:
    values = dict(
        id=5,
        pod_name="checkout",
        container_name="app",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="checkout",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=250,
        potential_savings_cost=100.0,
        expires_at=EXPIRES,
        action=ActionPlanV3(
            action_id="act-5",
            recommendation_ids=[5],
            target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="prod", name="checkout"),
            operations=[SetContainerResources(container="app", resources=ContainerResources(requests={"cpu": "250m"}))],
            constraints={"container_targeted": True},
            evidence_ref="recommendation-evidence/5",
        ),
    )
    values.update(overrides)
    return RecommendationRecord(**values)


def _pv(**overrides) -> RecommendationRecord:
    values = dict(
        id=6,
        pod_name="pv-orphan",
        type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        description="PersistentVolume 'pv-orphan' appears unused",
        scope="cluster",
        potential_savings_cost=12.0,
        expires_at=EXPIRES,
    )
    values.update(overrides)
    return RecommendationRecord(**values)


def test_a_typed_rightsizing_becomes_a_strategic_kubectl_patch() -> None:
    guide = manual_guide(_rightsizing())

    assert guide.mode == "patch" and guide.action_kind == "container_resources"
    assert guide.patch == {
        "spec": {"template": {"spec": {"containers": [{"name": "app", "resources": {"requests": {"cpu": "250m"}}}]}}}
    }
    dry_run, apply, rollout = (command.command for command in guide.commands)
    assert dry_run.endswith("--dry-run=server -o yaml")
    argv = shlex.split(apply)
    assert argv[:6] == ["kubectl", "patch", "deployment", "checkout", "-n", "prod"]
    assert argv[6:8] == ["--type", "strategic"]
    assert json.loads(argv[9]) == guide.patch
    assert rollout == "kubectl rollout status deployment checkout -n prod"
    assert guide.diff is not None and "-cpu: 500m" in guide.diff and "+cpu: 250m" in guide.diff
    assert guide.can_mark_applied


def test_an_orphaned_volume_becomes_a_kubectl_delete_with_the_retained_disk_note() -> None:
    guide = manual_guide(_pv())

    assert guide.mode == "delete" and guide.action_kind == "delete_resource"
    assert [command.command for command in guide.commands] == [
        "kubectl delete persistentvolume pv-orphan --dry-run=server",
        "kubectl delete persistentvolume pv-orphan",
    ]
    assert any("cannot be undone" in note for note in guide.notes)
    assert any("Retain" in note for note in guide.notes)


def test_names_are_shell_quoted() -> None:
    guide = manual_guide(_pv(pod_name="pv;rm -rf"))
    assert guide.commands[1].command == "kubectl delete persistentvolume 'pv;rm -rf'"


def test_other_kinds_are_advisory_and_inactive_ones_cannot_be_marked() -> None:
    guide = manual_guide(
        _rightsizing(type=RecommendationType.OFF_PEAK_SCALING, action=None, status=RecommendationStatus.APPLIED)
    )
    assert guide.mode == "advisory" and guide.commands == []
    assert not guide.can_mark_applied


class _Reader:
    """A live-resource reader double: returns ``live`` or raises ``error``."""

    def __init__(self, live=None, error: Exception | None = None) -> None:
        self.live, self.error, self.reads = live, error, []

    async def read_resource(self, ref):
        self.reads.append(ref)
        if self.error is not None:
            raise self.error
        return self.live


@pytest.fixture
def reader() -> _Reader:
    return _Reader()


@pytest.fixture
async def manual_client(reader):
    await db_manager.setup_sqlite(db_path=":memory:")
    repo = SQLiteRecommendationRepository(db_manager)
    await repo.save_recommendations([_rightsizing(id=None, action=None), _pv(id=None)])
    records = {record.type: record for record in await repo.get_active_recommendations()}
    app = create_app()
    app.dependency_overrides[get_recommendation_repository] = lambda: repo
    app.dependency_overrides[get_manual_resource_reader] = lambda: reader
    with TestClient(app) as client:
        yield client, repo, records
    app.dependency_overrides.clear()
    await db_manager.close()


async def test_the_guide_endpoint_and_mark_applied_record_a_measured_manual_change(manual_client, reader) -> None:
    client, repo, records = manual_client
    pv = records[RecommendationType.ORPHANED_PERSISTENT_VOLUME]

    guide = client.get(f"/api/v1/recommendations/{pv.id}/manual")
    assert guide.status_code == 200
    assert guide.json()["mode"] == "delete"

    applied = client.post(f"/api/v1/recommendations/{pv.id}/manual/applied")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "applied" and body["application_method"] == "manual"
    # A change id makes the change-measurement pipeline record and measure it like a PR merge.
    assert body["baseline"]["change_id"]
    # The absence was read from the live cluster before the change was recorded.
    assert [(ref.kind, ref.name) for ref in reader.reads] == [("PersistentVolume", "pv-orphan")]
    assert body["baseline"]["rollout"]["completion_source"] == "resource_absent"

    again = client.post(f"/api/v1/recommendations/{pv.id}/manual/applied")
    assert again.status_code == 409


async def test_a_manual_rightsizing_records_the_proposed_request_and_unknown_ids_are_404(manual_client) -> None:
    client, repo, records = manual_client
    cpu = records[RecommendationType.RIGHTSIZING_CPU]

    applied = client.post(f"/api/v1/recommendations/{cpu.id}/manual/applied")
    assert applied.status_code == 200
    assert applied.json()["actual_cpu_request_millicores"] == 250
    assert client.get("/api/v1/recommendations/999999/manual").status_code == 404
    assert client.post("/api/v1/recommendations/999999/manual/applied").status_code == 404


async def test_marking_a_deletion_applied_while_the_resource_still_exists_is_refused(manual_client, reader) -> None:
    client, repo, records = manual_client
    pv = records[RecommendationType.ORPHANED_PERSISTENT_VOLUME]
    reader.live = {"metadata": {"name": "pv-orphan"}, "status": {"phase": "Available"}}

    refused = client.post(f"/api/v1/recommendations/{pv.id}/manual/applied")

    assert refused.status_code == 409
    assert "still exists" in refused.json()["detail"]
    stored = await repo.get_recommendation_by_id(pv.id)
    assert stored is not None and stored.status == RecommendationStatus.ACTIVE
    assert stored.baseline is None or not getattr(stored.baseline, "change_id", None)


async def test_marking_a_deletion_applied_fails_closed_when_the_resource_cannot_be_read(manual_client, reader) -> None:
    client, repo, records = manual_client
    pv = records[RecommendationType.ORPHANED_PERSISTENT_VOLUME]
    reader.error = ResourceReadError("no Kubernetes configuration is available")

    refused = client.post(f"/api/v1/recommendations/{pv.id}/manual/applied")

    assert refused.status_code == 503
    stored = await repo.get_recommendation_by_id(pv.id)
    assert stored is not None and stored.status == RecommendationStatus.ACTIVE


async def test_a_manual_rightsizing_never_reads_the_cluster(manual_client, reader) -> None:
    client, repo, records = manual_client
    reader.error = ResourceReadError("must not be called")
    cpu = records[RecommendationType.RIGHTSIZING_CPU]
    assert client.post(f"/api/v1/recommendations/{cpu.id}/manual/applied").status_code == 200
    assert reader.reads == []


async def test_a_recommendation_with_a_plan_in_flight_cannot_be_marked_applied(manual_client) -> None:
    from types import SimpleNamespace

    from greenkube.api.dependencies import get_gitops_binding_repository
    from greenkube.gitops.contracts import ActionStatus, PlanStatus

    client, repo, records = manual_client
    cpu = records[RecommendationType.RIGHTSIZING_CPU]
    assert cpu.id is not None

    class _Plans:
        def __init__(self, status, plans) -> None:
            self.status, self.plans = status, plans

        async def get_recommendation_action_status(self, rec_id):
            return self.status

        async def list_plans_for_recommendation(self, rec_id):
            return self.plans

    overrides = client.app.dependency_overrides
    # An approved plan whose pull request is not open yet: the worker could still open it.
    overrides[get_gitops_binding_repository] = lambda: _Plans(
        ActionStatus.PLAN_READY, [SimpleNamespace(status=PlanStatus.APPROVED)]
    )
    refused = client.post(f"/api/v1/recommendations/{cpu.id}/manual/applied")
    assert refused.status_code == 409
    assert "plan in flight (approved)" in refused.json()["detail"]
    stored = await repo.get_recommendation_by_id(cpu.id)
    assert stored is not None and stored.status == RecommendationStatus.ACTIVE

    # A preview that nobody approved does not block applying by hand.
    overrides[get_gitops_binding_repository] = lambda: _Plans(
        ActionStatus.PLAN_READY, [SimpleNamespace(status=PlanStatus.VALIDATED)]
    )
    assert client.post(f"/api/v1/recommendations/{cpu.id}/manual/applied").status_code == 200
