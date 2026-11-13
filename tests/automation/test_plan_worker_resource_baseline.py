"""Plan worker checks of resource-bound kinds (H0c ``capture_baseline``, H0d bindings, H6)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest

from greenkube.automation.plan_worker import PlanWorker, PlanWorkerError
from greenkube.gitops.contracts import ActionPlanV3, ControllerKind, DeleteResource, PlanStatus, ResourceTarget
from greenkube.gitops.discovery.inventory import ResourceBinding
from greenkube.storage.resource_binding_repository import StoredResourceBinding
from tests.automation.pr_fixtures import plan as make_plan  # pyrefly: ignore[missing-import]
from tests.gitops.planning.test_raw_manifest import _source  # pyrefly: ignore[missing-import]

LB = ResourceTarget(api_version="v1", kind="Service", namespace="payments", name="old-lb")


def _delete_plan(status: PlanStatus = PlanStatus.APPROVED):
    plan = make_plan([DeleteResource()], recommendation_ids=(51,), baseline=False, status=status)
    action = ActionPlanV3(
        action_id="act-delete",
        recommendation_ids=[51],
        target=LB,
        operations=[DeleteResource()],
        evidence_ref="recommendation-evidence/51",
    )
    source = _source(namespace="payments", workload_kind="Service", workload_name="old-lb")
    return plan.model_copy(update={"action": action, "source": source})


class _Reader:
    def __init__(self, live: Any) -> None:
        self.live = live

    async def read_resource(self, ref: ResourceTarget):
        if isinstance(self.live, Exception):
            raise self.live
        return self.live


class _Bindings:
    def __init__(self, stored: Any) -> None:
        self.stored = stored

    async def get(self, cluster: str, ref: ResourceTarget):
        return self.stored


def _worker(reader: Any = None, bindings: Any = None) -> PlanWorker:
    unused = cast(Any, object())
    return PlanWorker(
        operations=unused,
        plans=unused,
        provider_resolver=unused,
        revision_resolver=None,
        pull_request_repository=unused,
        resource_reader=reader,
        resource_binding_reader=bindings,
    )


@pytest.mark.asyncio
async def test_a_present_resource_lets_the_deletion_proceed_without_a_workload_baseline() -> None:
    plan = _delete_plan()
    result = await _worker(_Reader({"metadata": {"name": "old-lb"}}))._ensure_pre_apply_baseline(None, plan)
    assert result is plan and result.pre_apply_baseline is None


@pytest.mark.asyncio
async def test_an_absent_resource_fails_with_no_effective_change_before_any_git_mutation() -> None:
    with pytest.raises(PlanWorkerError) as error:
        await _worker(_Reader(None))._ensure_pre_apply_baseline(None, _delete_plan())
    assert error.value.reason_code == "no_effective_change"
    assert error.value.plan_status is PlanStatus.FAILED
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_an_opted_out_live_resource_is_refused() -> None:
    live = {"metadata": {"annotations": {"greenkube.io/ignore": "true"}}}
    with pytest.raises(PlanWorkerError, match="opted out") as error:
        await _worker(_Reader(live))._ensure_pre_apply_baseline(None, _delete_plan())
    assert error.value.reason_code is None and error.value.plan_status is PlanStatus.FAILED


@pytest.mark.asyncio
async def test_an_unreadable_resource_or_missing_reader_is_retried() -> None:
    with pytest.raises(PlanWorkerError) as error:
        await _worker(_Reader(RuntimeError("timeout")))._ensure_pre_apply_baseline(None, _delete_plan())
    assert error.value.retryable
    with pytest.raises(PlanWorkerError) as missing:
        await _worker(None)._ensure_pre_apply_baseline(None, _delete_plan())
    assert missing.value.retryable


@pytest.mark.asyncio
async def test_an_applying_plan_is_not_checked_again() -> None:
    plan = _delete_plan(PlanStatus.APPLYING)
    assert await _worker(_Reader(None))._ensure_pre_apply_baseline(None, plan) is plan


def _stored(source) -> StoredResourceBinding:
    binding = ResourceBinding(
        ref=LB, status="resolved", controllers=((ControllerKind.ARGOCD, "argocd", "payments-prod"),), location=source
    )
    return StoredResourceBinding(binding=binding, observed_at=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_the_resource_binding_must_be_unchanged_at_apply_time() -> None:
    plan = _delete_plan()
    stored = _stored(_source())
    current = stored.binding.workload_source()
    assert current is not None
    plan = plan.model_copy(update={"source": current})
    await _worker(bindings=_Bindings(stored))._validate_current_binding(plan)

    moved = _stored(_source(path="apps/elsewhere"))
    with pytest.raises(PlanWorkerError, match="resource binding changed") as error:
        await _worker(bindings=_Bindings(moved))._validate_current_binding(plan)
    assert error.value.plan_status is PlanStatus.STALE
    with pytest.raises(PlanWorkerError):
        await _worker(bindings=_Bindings(None))._validate_current_binding(plan)
