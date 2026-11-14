"""Delivery safety (H6): a deletion is re-checked with the collectors' full orphan check."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.automation.controller import STEP_DELETION_RECHECK, AutomationController
from greenkube.automation.deletion_recheck import (
    DELETION_RECHECK_MARKER,
    DeletionOrphanChecker,
    OpenDeletionRechecker,
)
from greenkube.automation.git.base import CommentUpsertResult, GitProviderCapability, GitRepository
from greenkube.automation.plan_worker import PlanWorker, PlanWorkerError
from greenkube.collectors.k8s_object_reader import STATUS_UNREADABLE, StaticObjectReader
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    DeleteResource,
    PlanStatus,
    ResourceTarget,
)
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from tests.automation.pr_fixtures import plan as make_plan  # pyrefly: ignore[missing-import]
from tests.gitops.planning.test_raw_manifest import _source  # pyrefly: ignore[missing-import]

LB = ResourceTarget(api_version="v1", kind="Service", namespace="shop", name="old-lb")
PV = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-data")
REPO = GitRepository(provider="github", owner="acme", name="gitops")

SERVICE: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "old-lb", "namespace": "shop", "uid": "u-1"},
    "spec": {"type": "LoadBalancer", "selector": {"app": "old"}, "ports": [{"port": 80}]},
    "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.7"}]}},
}
HTTP_ROUTE: dict[str, Any] = {
    "apiVersion": "gateway.networking.k8s.io/v1",
    "kind": "HTTPRoute",
    "metadata": {"name": "shop", "namespace": "shop"},
    "spec": {"rules": [{"backendRefs": [{"name": "old-lb", "port": 80}]}]},
}
VOLUME: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "PersistentVolume",
    "metadata": {"name": "pv-data"},
    "spec": {
        "claimRef": {"namespace": "shop", "name": "data-old"},
        "csi": {"driver": "ebs.csi.aws.com", "volumeHandle": "vol-1"},
        "persistentVolumeReclaimPolicy": "Retain",
    },
    "status": {"phase": "Released"},
}


def _reader(**objects: list[dict[str, Any]]) -> StaticObjectReader:
    return StaticObjectReader({"services": [SERVICE], "persistentvolumes": [VOLUME], **objects})


def _checker(reader: StaticObjectReader) -> DeletionOrphanChecker:
    return DeletionOrphanChecker(lambda: reader)


# -- the orphan check ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unused_load_balancer_is_still_an_orphan():
    assert (await _checker(_reader()).check(LB)).status == "orphan"


@pytest.mark.asyncio
async def test_a_load_balancer_that_gained_an_http_route_reference_is_in_use():
    result = await _checker(_reader(httproutes=[HTTP_ROUTE])).check(LB)

    assert result.status == "in_use"
    assert "HTTPRoute shop/shop references it" in result.reason


@pytest.mark.asyncio
async def test_a_load_balancer_that_gained_endpoints_or_a_gateway_label_is_in_use():
    endpoints = {"metadata": {"name": "old-lb", "namespace": "shop"}, "subsets": [{"addresses": [{"ip": "10.0.0.4"}]}]}
    assert (await _checker(_reader(endpoints=[endpoints])).check(LB)).status == "in_use"

    labelled: dict[str, Any] = deepcopy(SERVICE)
    labelled["metadata"]["labels"] = {"gateway.networking.k8s.io/gateway-name": "shop"}
    assert (await _checker(StaticObjectReader({"services": [labelled]})).check(LB)).status == "in_use"


@pytest.mark.asyncio
async def test_an_unreadable_dependency_is_unknown_never_unused():
    reader = StaticObjectReader({"services": [SERVICE]}, statuses={"httproutes": STATUS_UNREADABLE})
    assert (await _checker(reader).check(LB)).status == "unknown"
    reader = StaticObjectReader({"services": [SERVICE]}, statuses={"services": STATUS_UNREADABLE})
    assert (await _checker(reader).check(LB)).status == "unknown"


@pytest.mark.asyncio
async def test_a_reader_error_is_unknown():
    class Broken:
        async def list(self, *args: Any, **kwargs: Any):
            raise RuntimeError("boom")

    assert (await DeletionOrphanChecker(lambda: Broken()).check(LB)).status == "unknown"


@pytest.mark.asyncio
async def test_a_deleted_resource_is_absent():
    assert (await _checker(StaticObjectReader({"services": []})).check(LB)).status == "absent"


@pytest.mark.asyncio
async def test_persistent_volume_criteria_are_rechecked():
    assert (await _checker(_reader()).check(PV)).status == "orphan"

    bound = deepcopy(VOLUME)
    bound["status"]["phase"] = "Bound"
    assert (await _checker(StaticObjectReader({"persistentvolumes": [bound]})).check(PV)).status == "in_use"

    claim = {"metadata": {"name": "data-old", "namespace": "shop"}}
    assert (await _checker(_reader(persistentvolumeclaims=[claim])).check(PV)).status == "in_use"

    snapshot = {
        "metadata": {"name": "snap", "namespace": "shop"},
        "spec": {"source": {"persistentVolumeClaimName": "data-old"}},
    }
    assert (await _checker(_reader(volumesnapshots=[snapshot])).check(PV)).status == "in_use"

    unreadable = StaticObjectReader({"persistentvolumes": [VOLUME]}, statuses={"volumesnapshots": STATUS_UNREADABLE})
    assert (await _checker(unreadable).check(PV)).status == "unknown"


# -- the worker refuses a deletion that is no longer an orphan -------------------------------


def _delete_plan(status: PlanStatus = PlanStatus.APPROVED, *, delivery_mode: str | None = None):
    plan = make_plan([DeleteResource()], recommendation_ids=(51,), baseline=False, status=status)
    action = ActionPlanV3(
        action_id="act-delete",
        recommendation_ids=[51],
        target=LB,
        operations=[DeleteResource()],
        evidence_ref="recommendation-evidence/51",
    )
    source = _source(namespace="shop", workload_kind="Service", workload_name="old-lb")
    return plan.model_copy(update={"action": action, "source": source, "delivery_mode": delivery_mode})


class _LiveReader:
    async def read_resource(self, ref: ResourceTarget):
        return SERVICE


def _worker(checker: DeletionOrphanChecker | None, **kwargs: Any) -> PlanWorker:
    unused = cast(Any, object())
    values: dict[str, Any] = {
        "operations": unused,
        "plans": unused,
        "provider_resolver": unused,
        "revision_resolver": None,
        "pull_request_repository": unused,
        "resource_reader": _LiveReader(),
        "deletion_orphan_checker": checker,
    }
    values.update(kwargs)
    return PlanWorker(**values)


@pytest.mark.asyncio
async def test_the_worker_refuses_a_load_balancer_that_gained_a_route_before_any_git_write():
    worker = _worker(_checker(_reader(httproutes=[HTTP_ROUTE])))

    with pytest.raises(PlanWorkerError, match="HTTPRoute") as error:
        await worker._ensure_pre_apply_baseline(None, _delete_plan())

    assert error.value.plan_status is PlanStatus.FAILED
    assert error.value.reason_code == "deletion_target_in_use"
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_the_worker_retries_when_the_orphan_check_cannot_read_the_cluster():
    reader = StaticObjectReader({"services": [SERVICE]}, statuses={"endpointslices": STATUS_UNREADABLE})
    with pytest.raises(PlanWorkerError) as unknown:
        await _worker(_checker(reader))._ensure_pre_apply_baseline(None, _delete_plan())
    assert unknown.value.retryable

    with pytest.raises(PlanWorkerError) as missing:
        await _worker(None)._ensure_pre_apply_baseline(None, _delete_plan())
    assert missing.value.retryable


@pytest.mark.asyncio
async def test_the_worker_lets_a_still_orphaned_deletion_proceed():
    plan = _delete_plan()
    assert await _worker(_checker(_reader()))._ensure_pre_apply_baseline(None, plan) is plan


# -- open deletion pull requests are invalidated -----------------------------------------------


class _Plans:
    def __init__(self, plan) -> None:
        self.plans = {plan.plan_id: plan}
        self.action_status: dict[int, ActionStatus] = {51: ActionStatus.PR_OPEN}
        self.reasons: list[str | None] = []

    async def list_plans(self, *, status=None, source_id=None):
        return [plan for plan in self.plans.values() if status is None or plan.status is status]

    async def set_plan_status(self, plan_id, status, *, expected_statuses=None, expected_digest=None, **kwargs):
        plan = self.plans.get(plan_id)
        if plan is None or (expected_statuses and plan.status not in expected_statuses):
            return None
        if expected_digest is not None and plan.digest != expected_digest:
            return None
        self.reasons.append(kwargs.get("reason_code"))
        updated = plan.model_copy(update={"status": status})
        self.plans[plan_id] = updated
        return updated

    async def set_recommendation_action_status(self, ids, status, *, expected_statuses=None, **kwargs):
        for recommendation_id in ids:
            if expected_statuses is None or self.action_status.get(recommendation_id) in expected_statuses:
                self.action_status[recommendation_id] = status
        return len(ids)


class _Provider:
    name = "github"

    def __init__(self, *, can_close: bool) -> None:
        self.comments: list[tuple[int, str, str]] = []
        self.closed: list[int] = []
        if can_close:
            self.close_pull_request = self._close

    def supports(self, capability) -> bool:
        return capability == GitProviderCapability.PULL_REQUEST_COMMENTS

    async def upsert_pull_request_comment(self, repo, number, *, marker, body):
        self.comments.append((number, marker, body))
        return CommentUpsertResult.CREATED, 1

    async def _close(self, repo, number):
        self.closed.append(number)

    async def close(self) -> None:
        return None


class _PullRequests:
    async def get_pull_requests_for_recommendation(self, recommendation_id: int):
        return [
            PullRequestRecord(
                id=1,
                provider="github",
                recommendation_id=51,
                plan_id="plan-delete",
                repo="acme/gitops",
                pr_number=12,
                pr_url="https://github.com/acme/gitops/pull/12",
                status=PullRequestStatus.OPEN,
            )
        ]


class _Recommendations:
    def __init__(self) -> None:
        self.transitions: list[tuple[int, dict[str, Any], str, list[str]]] = []

    async def transition_and_record_event(self, rec_id, changes, event_type, actor, payload, *, expected_statuses):
        self.transitions.append((rec_id, changes, event_type, expected_statuses))
        return SimpleNamespace(id=rec_id)


def _open_deletion(**kwargs: Any):
    plan = _delete_plan(PlanStatus.PR_OPEN, **kwargs)
    return plan.model_copy(
        update={"plan_id": "plan-delete", "file_plan": plan.file_plan.model_copy(update={"plan_id": "plan-delete"})}
    )


def _rechecking_worker(reader: StaticObjectReader, *, can_close: bool = True, **plan_kwargs: Any):
    plan = _open_deletion(**plan_kwargs)
    plans = _Plans(plan)
    provider = _Provider(can_close=can_close)
    recommendations = _Recommendations()

    async def resolve(source):
        return SimpleNamespace(provider=provider, repository=REPO)

    worker = _worker(
        _checker(reader),
        plans=plans,
        provider_resolver=SimpleNamespace(resolve=resolve),
        pull_request_repository=_PullRequests(),
        recommendation_repository=recommendations,
    )
    return worker, plans, provider, recommendations


@pytest.mark.asyncio
async def test_an_open_deletion_pr_whose_load_balancer_gains_an_http_route_is_invalidated():
    worker, plans, provider, recommendations = _rechecking_worker(_reader(httproutes=[HTTP_ROUTE]))

    assert await worker.recheck_open_deletions() == 1

    assert plans.plans["plan-delete"].status is PlanStatus.STALE
    assert plans.reasons == ["deletion_target_in_use"]
    assert plans.action_status[51] is ActionStatus.NONE
    number, marker, body = provider.comments[0]
    assert (number, marker) == (12, DELETION_RECHECK_MARKER)
    assert "HTTPRoute shop/shop references it" in body and "Do not merge" in body
    assert provider.closed == [12]
    assert recommendations.transitions[0][0] == 51
    assert recommendations.transitions[0][1]["status"] == "stale"
    assert recommendations.transitions[0][2] == "retired"


@pytest.mark.asyncio
async def test_a_provider_that_cannot_close_still_gets_the_comment_and_the_plan_is_stale():
    worker, plans, provider, _ = _rechecking_worker(_reader(httproutes=[HTTP_ROUTE]), can_close=False)

    assert await worker.recheck_open_deletions() == 1

    assert provider.comments and provider.closed == []
    assert plans.plans["plan-delete"].status is PlanStatus.STALE


@pytest.mark.asyncio
async def test_an_external_delivery_is_not_written_to_but_the_plan_is_stale():
    worker, plans, provider, _ = _rechecking_worker(_reader(httproutes=[HTTP_ROUTE]), delivery_mode="patch")

    assert await worker.recheck_open_deletions() == 1

    assert provider.comments == [] and provider.closed == []
    assert plans.plans["plan-delete"].status is PlanStatus.STALE


@pytest.mark.asyncio
async def test_an_orphan_or_unreadable_target_leaves_the_pull_request_open():
    worker, plans, provider, _ = _rechecking_worker(_reader())
    assert await worker.recheck_open_deletions() == 0
    assert plans.plans["plan-delete"].status is PlanStatus.PR_OPEN

    unreadable = StaticObjectReader({"services": [SERVICE]}, statuses={"ingresses": STATUS_UNREADABLE})
    worker, plans, provider, _ = _rechecking_worker(unreadable)
    assert await worker.recheck_open_deletions() == 0
    assert plans.plans["plan-delete"].status is PlanStatus.PR_OPEN
    assert provider.comments == []


@pytest.mark.asyncio
async def test_rechecker_ignores_non_deletion_plans():
    plan = make_plan(status=PlanStatus.PR_OPEN)
    plans = _Plans(plan)
    rechecker = OpenDeletionRechecker(SimpleNamespace(plans=plans), _checker(_reader(httproutes=[HTTP_ROUTE])))

    assert await rechecker.recheck_once() == 0
    assert plans.plans[plan.plan_id].status is PlanStatus.PR_OPEN


def test_the_controller_registers_the_periodic_deletion_recheck():
    worker = SimpleNamespace(recheck_open_deletions=lambda: None)
    controller = AutomationController(
        plan_service=cast(Any, object()),
        lease_repository=cast(Any, object()),
        plan_worker=worker,
        delivery_enabled=True,
    )

    assert STEP_DELETION_RECHECK in [step for step, _ in controller._steps()]
    assert controller.intervals[STEP_DELETION_RECHECK] == 300.0
