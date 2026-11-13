"""A PersistentVolume deletion plan through the real plan service (roadmap H6 acceptance, planning half)."""

from __future__ import annotations

import pytest

from greenkube.gitops.contracts import ActionPlanV3, ActionStatus, DeleteResource, PlanStatus, ResourceTarget
from greenkube.gitops.discovery.inventory import CLUSTER_SCOPE_NAMESPACE, CONTROLLER_PRUNE_EVIDENCE
from tests.automation.test_plan_service import (  # pyrefly: ignore[missing-import]
    BASE_REVISION,
    FakeGitBlobResolver,
    MemoryPlanRepository,
    _archive_snapshot,
    _service,
    _source,
)

PV_MANIFEST = """apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-orphan
spec:
  capacity:
    storage: 10Gi
  persistentVolumeReclaimPolicy: Delete
  accessModes: [ReadWriteOnce]
"""


@pytest.mark.asyncio
async def test_a_persistent_volume_deletion_becomes_a_validated_file_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(
        id="argocd-pv",
        namespace=CLUSTER_SCOPE_NAMESPACE,
        workload_kind="PersistentVolume",
        workload_name="pv-orphan",
        evidence={CONTROLLER_PRUNE_EVIDENCE: True, "resource_binding": True},
    )
    action = ActionPlanV3(
        action_id="act-pv",
        recommendation_ids=[42],
        target=ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-orphan"),
        operations=[DeleteResource()],
        constraints={"resource_deletion": True},
        evidence_ref="recommendation-evidence/42",
    )
    repository = MemoryPlanRepository()
    service = _service(repository, FakeGitBlobResolver(content=PV_MANIFEST))

    plan = await service.create_plan(
        action, source, _archive_snapshot(monkeypatch, source, PV_MANIFEST), policy_result={"allowed": True}
    )

    assert plan.status is PlanStatus.VALIDATED
    assert plan.file_plan is not None and plan.file_plan.base_revision == BASE_REVISION
    (edit,) = plan.file_plan.files
    assert (edit.path, edit.operation) == ("apps/payments/deployment.yaml", "delete")
    assert plan.file_plan.validation.passed
    assert not any(warning.startswith("persistent_volume_retained") for warning in plan.file_plan.warnings)
    assert repository.action_status[42] is ActionStatus.PLAN_READY
