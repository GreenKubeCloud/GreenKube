"""Plan worker checks of resource-bound kinds (H0c ``capture_baseline``, H0d bindings, H6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from greenkube.automation.plan_worker import PlanWorker, PlanWorkerError
from greenkube.collectors.lb_collector import OrphanCheck
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ControllerKind,
    DeleteResource,
    PlanStatus,
    ResourceTarget,
    configured_binding_ttl,
)
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


class _StillOrphaned:
    async def check(self, target: ResourceTarget) -> OrphanCheck:
        return OrphanCheck("orphan")


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
        deletion_orphan_checker=cast(Any, _StillOrphaned()),
    )


@pytest.mark.asyncio
async def test_a_present_resource_lets_the_deletion_proceed_without_a_workload_baseline() -> None:
    plan = _delete_plan()
    result = await _worker(
        _Reader({"metadata": {"name": "old-lb"}, "spec": {"type": "LoadBalancer"}})
    )._ensure_pre_apply_baseline(None, plan)
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
async def test_a_service_that_is_no_longer_a_load_balancer_fails_closed() -> None:
    live = {"metadata": {"name": "old-lb"}, "spec": {"type": "ClusterIP"}}
    with pytest.raises(PlanWorkerError, match="no longer a LoadBalancer") as error:
        await _worker(_Reader(live))._ensure_pre_apply_baseline(None, _delete_plan())
    assert error.value.plan_status is PlanStatus.FAILED and not error.value.retryable


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


def _stored(source, *, prune=None, observed_at=None) -> StoredResourceBinding:
    binding = ResourceBinding(
        ref=LB,
        status="resolved",
        controllers=((ControllerKind.ARGOCD, "argocd", "payments-prod"),),
        location=source,
        prune=prune,
    )
    return StoredResourceBinding(binding=binding, observed_at=observed_at or datetime.now(timezone.utc))


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


@pytest.mark.asyncio
async def test_a_missing_binding_reader_is_retried_instead_of_skipping_the_check() -> None:
    with pytest.raises(PlanWorkerError, match="no resource binding reader") as error:
        await _worker()._validate_current_binding(_delete_plan())
    assert error.value.retryable


def _approved_on(stored: StoredResourceBinding):
    current = stored.binding.workload_source()
    assert current is not None
    return _delete_plan().model_copy(update={"source": current})


@pytest.mark.asyncio
async def test_a_stale_resource_binding_makes_the_plan_stale_at_apply_time() -> None:
    plan = _approved_on(_stored(_source()))
    old = _stored(_source(), observed_at=datetime.now(timezone.utc) - configured_binding_ttl() - timedelta(minutes=1))
    with pytest.raises(PlanWorkerError, match="binding TTL") as error:
        await _worker(bindings=_Bindings(old))._validate_current_binding(plan)
    assert error.value.plan_status is PlanStatus.STALE


@pytest.mark.asyncio
async def test_prune_disabled_after_approval_makes_the_plan_stale() -> None:
    plan = _approved_on(_stored(_source(), prune=True))
    await _worker(bindings=_Bindings(_stored(_source(), prune=True)))._validate_current_binding(plan)
    with pytest.raises(PlanWorkerError, match="stopped pruning") as error:
        await _worker(bindings=_Bindings(_stored(_source(), prune=False)))._validate_current_binding(plan)
    assert error.value.plan_status is PlanStatus.STALE
