"""Re-check that a resource proposed for deletion is still unused (H6, delivery safety).

A deletion recommendation is built from a full orphan check (endpoints and
EndpointSlices, pods and workload templates, Gateway/Ingress/route references,
gateway and mesh labels for a LoadBalancer Service; phase, claim, StatefulSet and
snapshot dependencies for a PersistentVolume). Days can pass between that check, the
approval and the merge of the deletion pull request. :class:`DeletionOrphanChecker`
runs the same check again on the live object:

* the plan worker runs it before it creates the branch (a resource in use is refused);
* :class:`OpenDeletionRechecker` runs it periodically for every open deletion pull
  request; a resource that is in use again makes the plan stale, the pull request
  gets a GreenKube comment and is closed when the Git provider can close it.

An unreadable dependency (RBAC, timeout) is never read as "unused" (fail closed): the
worker retries instead of opening the pull request, and an open pull request is left
as it is and re-checked on the next pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any, cast

from greenkube.collectors.lb_collector import ORPHAN, ORPHAN_ABSENT, ORPHAN_IN_USE, ORPHAN_UNKNOWN, OrphanCheck
from greenkube.gitops.contracts import ActionStatus, Plan, PlanStatus, ResourceTarget

logger = logging.getLogger(__name__)

#: Action kind whose open pull requests are re-checked.
DELETE_RESOURCE_KIND = "delete_resource"
#: Plan status reason of a deletion whose target is in use again.
DELETION_TARGET_IN_USE = "deletion_target_in_use"
#: Marker of the GreenKube comment on an invalidated deletion pull request.
DELETION_RECHECK_MARKER = "<!-- greenkube:deletion-recheck -->"
#: Open deletion plans re-checked per pass, at most.
MAX_RECHECKS_PER_PASS = 20

__all__ = [
    "DELETION_RECHECK_MARKER",
    "DELETION_TARGET_IN_USE",
    "ORPHAN",
    "ORPHAN_ABSENT",
    "ORPHAN_IN_USE",
    "ORPHAN_UNKNOWN",
    "DeletionOrphanChecker",
    "OpenDeletionRechecker",
    "OrphanCheck",
    "is_deletion_plan",
]


def _describe(target: ResourceTarget) -> str:
    return f"{target.kind} {target.namespace + '/' if target.namespace else ''}{target.name}"


def is_deletion_plan(plan: Plan) -> bool:
    from greenkube.automation.actions import action_kind_of

    spec = action_kind_of(plan.action)
    return spec is not None and spec.kind == DELETE_RESOURCE_KIND


class DeletionOrphanChecker:
    """Runs the collectors' full orphan check on the live deletion target.

    ``reader_factory`` returns a fresh object reader for each check (the Kubernetes
    reader caches listings for the lifetime of one scan).
    """

    def __init__(self, reader_factory: Callable[[], Any] | None = None) -> None:
        self._reader_factory = reader_factory

    def _reader(self) -> Any:
        if self._reader_factory is not None:
            return self._reader_factory()
        from greenkube.collectors.k8s_object_reader import KubernetesObjectReader

        return KubernetesObjectReader()

    async def check(self, target: ResourceTarget) -> OrphanCheck:
        from greenkube.collectors.lb_collector import check_load_balancer
        from greenkube.collectors.pv_collector import check_persistent_volume

        if (target.api_version, target.kind) == ("v1", "Service") and target.namespace:
            kind, namespace = "services", target.namespace
        elif (target.api_version, target.kind) == ("v1", "PersistentVolume") and not target.namespace:
            kind, namespace = "persistentvolumes", None
        else:
            return OrphanCheck(ORPHAN_UNKNOWN, f"GreenKube cannot re-check {target.api_version}/{target.kind}")
        try:
            reader = self._reader()
            listing = await reader.list(kind, namespace)
            if listing.status != "ok":
                return OrphanCheck(ORPHAN_UNKNOWN, f"cannot read {kind} ({listing.detail})")
            live = next(
                (item for item in listing.items if ((item.get("metadata") or {}).get("name")) == target.name),
                None,
            )
            if live is None:
                return OrphanCheck(ORPHAN_ABSENT, f"{_describe(target)} no longer exists")
            if kind == "services":
                return await check_load_balancer(reader, live)
            return await check_persistent_volume(reader, live)
        except Exception as exc:  # fail closed: an error is never "unused"
            return OrphanCheck(ORPHAN_UNKNOWN, f"the live state could not be read ({type(exc).__name__})")


class OpenDeletionRechecker:
    """Invalidates open deletion pull requests whose resource is in use again."""

    def __init__(
        self,
        worker: Any,
        checker: DeletionOrphanChecker,
        *,
        clock: Callable[[], datetime] | None = None,
        max_per_pass: int = MAX_RECHECKS_PER_PASS,
    ) -> None:
        self.worker = worker
        self.checker = checker
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_per_pass = max_per_pass
        self._cursor = 0

    async def recheck_once(self) -> int:
        """Re-check one bounded, rotating page of open deletion plans; returns how many were invalidated."""
        lister = getattr(self.worker.plans, "list_plans", None)
        if not callable(lister):
            return 0
        plans = [plan for plan in await cast(Any, lister)(status=PlanStatus.PR_OPEN) if is_deletion_plan(plan)]
        if not plans:
            return 0
        offset = self._cursor % len(plans)
        selected = (plans[offset:] + plans[:offset])[: self.max_per_pass]
        self._cursor = offset + len(selected)
        invalidated = 0
        for plan in selected:
            result = await self.checker.check(plan.action.target)
            if result.status == ORPHAN_IN_USE:
                if await self.invalidate(plan, result.reason):
                    invalidated += 1
            elif result.status == ORPHAN_UNKNOWN:
                logger.warning(
                    "Open deletion plan %s: %s could not be re-checked (%s); the pull request is left open and "
                    "re-checked on the next pass.",
                    plan.plan_id,
                    _describe(plan.action.target),
                    result.reason,
                )
        return invalidated

    async def invalidate(self, plan: Plan, reason: str) -> bool:
        """Mark the plan stale, then comment on and close its pull request (best effort) and retire it.

        The compare-and-swap comes first: a plan that polling already moved to merged or
        closed is left alone, so no "do not merge" comment lands on a finished pull request.
        """
        target = plan.action.target
        logger.warning(
            "Open deletion plan %s: %s is in use again (%s); invalidating its pull request.",
            plan.plan_id,
            _describe(target),
            reason,
        )
        stale = await self.worker.plans.set_plan_status(
            plan.plan_id,
            PlanStatus.STALE,
            expected_statuses=(PlanStatus.PR_OPEN,),
            expected_digest=plan.digest,
            actor="automation-controller",
            reason_code=DELETION_TARGET_IN_USE,
        )
        if stale is None:
            return False
        closed = await self._comment_and_close(plan, reason)
        await self.worker.plans.set_recommendation_action_status(
            plan.recommendation_ids,
            ActionStatus.NONE,
            expected_statuses=(ActionStatus.PR_OPEN, ActionStatus.PLAN_READY),
            actor="automation-controller",
            plan_id=plan.plan_id,
        )
        await self._retire_recommendations(plan, reason, closed)
        return True

    async def _comment_and_close(self, plan: Plan, reason: str) -> bool:
        from greenkube.automation.delivery import is_external
        from greenkube.automation.git.base import GitProviderCapability

        if is_external(getattr(plan, "delivery_mode", None)):
            # patch/ci delivery: the token in the cluster is read-only; the plan status explains it.
            return False
        records = await self.worker._plan_pull_request_records(plan)
        record = next((item for item in records if item.pr_number is not None), None)
        if record is None:
            return False
        try:
            target = await self.worker.provider_resolver.resolve(plan.source)
        except Exception as exc:
            logger.info("Deletion re-check: provider unavailable for plan %s (%s).", plan.plan_id, type(exc).__name__)
            return False
        provider = target.provider
        closed = False
        try:
            if provider.supports(GitProviderCapability.PULL_REQUEST_COMMENTS):
                await provider.upsert_pull_request_comment(
                    target.repository,
                    record.pr_number,
                    marker=DELETION_RECHECK_MARKER,
                    body=self._comment(plan, reason),
                )
            closer = getattr(provider, "close_pull_request", None)
            if callable(closer):
                await cast(Any, closer)(target.repository, record.pr_number)
                closed = True
        except Exception as exc:  # best effort: the plan is made stale regardless
            logger.warning(
                "Deletion re-check: could not comment on or close pull request #%s of plan %s (%s).",
                record.pr_number,
                plan.plan_id,
                type(exc).__name__,
            )
        finally:
            try:
                await provider.close()
            except Exception:
                pass
        return closed

    @staticmethod
    def _comment(plan: Plan, reason: str) -> str:
        return "\n".join(
            (
                DELETION_RECHECK_MARKER,
                "### GreenKube: this deletion is no longer safe",
                "",
                f"`{_describe(plan.action.target)}` is in use again: {reason}.",
                "",
                f"GreenKube re-checked it while this pull request was open and marked plan `{plan.plan_id}` stale. "
                "**Do not merge it**: merging would delete a resource that is used. Close this pull request; "
                "GreenKube recommends the deletion again only after the resource has looked unused for the "
                "configured minimum period.",
            )
        )

    async def _retire_recommendations(self, plan: Plan, reason: str, closed: bool) -> None:
        repository = getattr(self.worker, "recommendation_repository", None)
        transition = getattr(repository, "transition_and_record_event", None)
        if not callable(transition):
            return
        payload = {
            "code": DELETION_TARGET_IN_USE,
            "reason": f"{_describe(plan.action.target)} is in use again: {reason}",
            "plan_id": plan.plan_id,
            "pull_request_closed": closed,
        }
        for recommendation_id in cast(Sequence[int], plan.recommendation_ids):
            try:
                await cast(Any, transition)(
                    recommendation_id,
                    {"status": "stale", "updated_at": self.clock()},
                    "retired",
                    "automation-controller",
                    payload,
                    expected_statuses=["pr_open", "active"],
                )
            except Exception as exc:
                logger.warning("Deletion re-check: could not retire recommendation %s (%s).", recommendation_id, exc)
