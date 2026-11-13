"""Keep one GreenKube status comment up to date on plan PRs (roadmap J5).

The publisher only reads state other components already persist: the PR
record (merge and sync, written by the lifecycle reconciler and the sync
observer), the change-measurement records (applied, measuring, outcome) and,
when change measurement is off, the recommendation's verification fields. It
never changes that state.

Every publication renders the whole comment. A body identical to the last one
published for the plan costs no provider call; otherwise the provider edits
the comment that starts with the GreenKube marker, or creates it once.

Merged PRs get the comment within the lookback window. An **open** PR gets it
only when newer advice is queued behind its plan (E7): "newer advice available"
then shows before the merge. Each open plan is checked at most once per
``open_check_interval`` and every cycle shares one cap on provider writes.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast

from greenkube.automation.delivery import DELIVERY_DIRECT, configured_delivery_mode, is_external
from greenkube.automation.git.base import CommentUpsertResult, GitProviderCapability
from greenkube.automation.pr_content.lifecycle import (
    LIFECYCLE_MARKER,
    LifecycleState,
    MeasuredQuantity,
    comment_digest,
    render_lifecycle_comment,
)
from greenkube.automation.pr_content.model import PullRequestRenderSettings
from greenkube.gitops.contracts import ControllerKind, Plan
from greenkube.models.metrics import PullRequestRecord, RecommendationRecord, RecommendationStatus
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE

logger = logging.getLogger(__name__)

DEFAULT_FEEDBACK_LOOKBACK_DAYS = 30
#: Plans whose comment needs a provider call in one reconciliation cycle, at most.
MAX_PUBLICATIONS_PER_CYCLE = 25
#: An open plan PR is checked for newer advice at most this often.
DEFAULT_OPEN_CHECK_INTERVAL = timedelta(minutes=15)
#: PR record statuses of a PR that is still open (not merged, not closed).
_OPEN_STATUSES = {"open", "pending"}
_CONTROLLER_LABELS = {
    ControllerKind.ARGOCD: "Argo CD",
    ControllerKind.FLUX_KUSTOMIZATION: "Flux Kustomization",
    ControllerKind.FLUX_HELM_RELEASE: "Flux HelmRelease",
    ControllerKind.MANUAL_RESOLUTION: "the GitOps controller",
}
_DECIDED_STATES = {"realized", "not_realized", "inconclusive", "contaminated", "regressed"}
_VERIFICATION_OUTCOMES = {"passed": "realized", "failed": "not_realized", "inconclusive": "inconclusive"}


class FeedbackOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"


class FeedbackPullRequestRepository(Protocol):
    async def list_pull_requests_by_plan_id(self, plan_id: str) -> Sequence[PullRequestRecord]: ...

    async def list_feedback_pull_requests(
        self,
        *,
        merged_since: datetime,
        limit: int,
        after_id: int | None = None,
    ) -> Sequence[PullRequestRecord]: ...


class OpenPullRequestRepository(Protocol):
    """Open plan PRs (the PR poller's listing), used to report newer advice before the merge."""

    async def list_pollable_pull_requests(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> Sequence[PullRequestRecord]: ...


class FeedbackPlanRepository(Protocol):
    async def get_plan(self, plan_id: str) -> Plan | None: ...


class FeedbackRecommendationRepository(Protocol):
    async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None: ...


class FeedbackProviderResolver(Protocol):
    async def resolve(self, source: Any) -> Any: ...


#: Optional hook returning a "newer advice available" note for a plan (E7 revisions).
NewerAdviceHook = Callable[[Plan, Sequence[RecommendationRecord]], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class FeedbackCycleSummary:
    plans: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: int = 0


class PullRequestFeedbackPublisher:
    """Render and publish the lifecycle comment of one plan PR."""

    def __init__(
        self,
        *,
        pull_request_repository: FeedbackPullRequestRepository,
        plan_repository: FeedbackPlanRepository,
        provider_resolver: FeedbackProviderResolver,
        recommendation_repository: FeedbackRecommendationRepository | None = None,
        change_repository: Any | None = None,
        settings: PullRequestRenderSettings | None = None,
        newer_advice: NewerAdviceHook | None = None,
        lookback_days: int = DEFAULT_FEEDBACK_LOOKBACK_DAYS,
        clock: Callable[[], datetime] | None = None,
        cache_size: int = 2048,
        open_check_interval: timedelta = DEFAULT_OPEN_CHECK_INTERVAL,
        read_only: bool | Callable[[], bool] = False,
    ) -> None:
        self.pull_request_repository = pull_request_repository
        self.plan_repository = plan_repository
        self.provider_resolver = provider_resolver
        self.recommendation_repository = recommendation_repository
        self.change_repository = change_repository
        self.settings = settings or PullRequestRenderSettings()
        self.newer_advice = newer_advice
        self.lookback_days = lookback_days
        self.clock = clock or (lambda: datetime.now(UTC))
        self.cache_size = cache_size
        self._published: OrderedDict[str, str] = OrderedDict()
        self._after_id: int | None = None
        self.open_check_interval = open_check_interval
        self._open_after_id: int | None = None
        #: When each open plan was last checked for newer advice (bounded like ``_published``).
        self._open_checked: OrderedDict[str, datetime] = OrderedDict()
        #: Credentials are read-only (``patch``/``ci`` delivery, F4): never write to the provider.
        self._read_only = read_only

    def writes_allowed(self, plan: Plan) -> bool:
        """Whether the comment may be written for ``plan``.

        In ``patch`` and ``ci`` delivery modes GreenKube needs read access only;
        the token in the cluster is expected to be read-only, so a comment write
        would only fail (and could mark the provider in error). A plan approved
        for ``patch`` or ``ci`` is never commented either, whatever the current
        mode, since its pull request was opened by someone else.
        """
        read_only = self._read_only() if callable(self._read_only) else self._read_only
        return not read_only and not is_external(getattr(plan, "delivery_mode", None))

    async def publish(self, plan_id: str) -> FeedbackOutcome:
        """Bring the plan's status comment up to date; never raises for unknown plans."""
        plan = await self.plan_repository.get_plan(plan_id)
        if plan is None or not self.writes_allowed(plan):
            return FeedbackOutcome.SKIPPED
        records = list(await self.pull_request_repository.list_pull_requests_by_plan_id(plan_id))
        record = _primary_record(records)
        if record is None or record.pr_number is None:
            return FeedbackOutcome.SKIPPED
        recommendations = await self._recommendations(plan)
        state = await self.lifecycle_state(plan, record, recommendations)
        if state.pr_status in _OPEN_STATUSES and not state.newer_advice:
            # Before the merge, the comment only carries newer advice (E7): nothing to say yet.
            return FeedbackOutcome.SKIPPED
        body = render_lifecycle_comment(state, self.settings)
        digest = comment_digest(body)
        if self._published.get(plan_id) == digest:
            self._published.move_to_end(plan_id)
            return FeedbackOutcome.UNCHANGED
        target = await self.provider_resolver.resolve(plan.source)
        provider = target.provider
        try:
            if record.repo != target.repository.full_name:
                logger.warning("Plan %s PR record does not match its repository; skipping its comment.", plan_id)
                return FeedbackOutcome.SKIPPED
            if not provider.supports(GitProviderCapability.PULL_REQUEST_COMMENTS):
                return FeedbackOutcome.UNSUPPORTED
            result, _ = await provider.upsert_pull_request_comment(
                target.repository, record.pr_number, marker=LIFECYCLE_MARKER, body=body
            )
        finally:
            try:
                await provider.close()
            except Exception:
                pass
        self._remember(plan_id, digest)
        return {
            CommentUpsertResult.CREATED: FeedbackOutcome.CREATED,
            CommentUpsertResult.UPDATED: FeedbackOutcome.UPDATED,
        }.get(result, FeedbackOutcome.UNCHANGED)

    async def reconcile_once(self, *, limit: int = MAX_PULL_REQUEST_POLL_BATCH_SIZE) -> FeedbackCycleSummary:
        """Refresh the comments of PRs merged within the lookback window, then of open PRs with
        newer advice (one bounded page each, one shared cap on provider writes)."""
        since = self.clock() - timedelta(days=self.lookback_days)
        records = await self.pull_request_repository.list_feedback_pull_requests(
            merged_since=since, limit=limit, after_id=self._after_id
        )
        if not records and self._after_id is not None:
            self._after_id = None
            records = await self.pull_request_repository.list_feedback_pull_requests(merged_since=since, limit=limit)
        ids = [record.id for record in records if isinstance(record.id, int)]
        self._after_id = max(ids) if len(records) == limit and ids else None
        plan_ids = list(dict.fromkeys(record.plan_id for record in records if record.plan_id))
        for plan_id in await self._open_plan_ids(limit):
            if plan_id not in plan_ids:
                plan_ids.append(plan_id)
        counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
        provider_writes = 0
        for plan_id in plan_ids:
            if provider_writes >= MAX_PUBLICATIONS_PER_CYCLE:
                counts["skipped"] += 1
                continue
            try:
                outcome = await self.publish(plan_id)
            except Exception as error:
                counts["errors"] += 1
                logger.warning("Could not refresh the status comment of plan %s (%s).", plan_id, type(error).__name__)
                continue
            if outcome in (FeedbackOutcome.CREATED, FeedbackOutcome.UPDATED):
                provider_writes += 1
            key = outcome.value if outcome.value in counts else "skipped"
            counts[key] += 1
        return FeedbackCycleSummary(plans=len(plan_ids), **counts)

    async def _open_plan_ids(self, limit: int) -> list[str]:
        """Open plan PRs due for a newer-advice check (none without the E7 hook)."""
        if self.newer_advice is None or not callable(
            getattr(self.pull_request_repository, "list_pollable_pull_requests", None)
        ):
            return []
        lister = cast(OpenPullRequestRepository, self.pull_request_repository).list_pollable_pull_requests
        try:
            records = list(await lister(limit=limit, after_id=self._open_after_id))
            if not records and self._open_after_id is not None:
                self._open_after_id = None
                records = list(await lister(limit=limit))
        except Exception as error:
            logger.warning("Could not list open plan pull requests (%s).", type(error).__name__)
            return []
        ids = [record.id for record in records if isinstance(record.id, int)]
        self._open_after_id = max(ids) if len(records) == limit and ids else None
        now = self.clock()
        due: list[str] = []
        for record in records:
            plan_id = record.plan_id
            if not plan_id or _status(record.status) not in _OPEN_STATUSES or plan_id in due:
                continue
            checked = self._open_checked.get(plan_id)
            if checked is not None and now - checked < self.open_check_interval:
                continue
            self._open_checked[plan_id] = now
            self._open_checked.move_to_end(plan_id)
            due.append(plan_id)
        while len(self._open_checked) > self.cache_size:
            self._open_checked.popitem(last=False)
        return due

    async def notify(self, plan_id: str) -> None:
        """Best-effort publication after a lifecycle event (sync observed, outcome decided)."""
        try:
            await self.publish(plan_id)
        except Exception as error:
            logger.warning("Could not update the status comment of plan %s (%s).", plan_id, type(error).__name__)

    async def lifecycle_state(
        self,
        plan: Plan,
        record: PullRequestRecord,
        recommendations: Sequence[RecommendationRecord],
    ) -> LifecycleState:
        source = plan.source
        controller = f"{_CONTROLLER_LABELS.get(source.controller_kind, source.controller_kind.value)} `" + (
            f"{source.controller_namespace}/{source.controller_name}`"
        )
        values: dict[str, Any] = {
            "plan_id": plan.plan_id,
            "pr_status": _status(record.status),
            "merged_at": record.merged_at,
            "merge_commit_sha": record.merge_commit_sha,
            "sync_status": record.sync_status,
            "sync_revision": record.sync_revision,
            "sync_observed_at": record.sync_observed_at,
            "sync_error_code": record.sync_error_code,
            "controller": controller,
            "currency": _currency(recommendations),
        }
        measured = await self._change_measurement(plan)
        if measured is not None:
            values.update(measured)
        else:
            values.update(_verification_state(recommendations))
        if plan.rollback_of_plan_id is None and any(
            record.status is RecommendationStatus.ROLLBACK_REVIEW for record in recommendations
        ):
            values["rollback_review"] = True
        if self.newer_advice is not None:
            try:
                values["newer_advice"] = await self.newer_advice(plan, recommendations)
            except Exception as error:
                logger.debug("Newer-advice hook failed for plan %s (%s).", plan.plan_id, type(error).__name__)
        return LifecycleState(**values)

    async def _recommendations(self, plan: Plan) -> list[RecommendationRecord]:
        if self.recommendation_repository is None:
            return []
        records = []
        for recommendation_id in plan.recommendation_ids:
            record = await self.recommendation_repository.get_recommendation_by_id(recommendation_id)
            if record is not None:
                records.append(record)
        return records

    async def _change_measurement(self, plan: Plan) -> dict[str, Any] | None:
        """Applied, measuring and outcome from the change-measurement records (C1-C5)."""
        repository = self.change_repository
        if repository is None:
            return None
        change_ids = await repository.change_ids_for_recommendations(list(plan.recommendation_ids))
        change = None
        for change_id in dict.fromkeys(change_ids.values()):
            candidate = await repository.get_change(change_id)
            if candidate is not None and candidate.plan_id == plan.plan_id:
                change = candidate
                break
        if change is None:
            return None
        status = await repository.get_status(change.change_id)
        values: dict[str, Any] = {"change_id": change.change_id, "applied_at": change.rollout_completed_at}
        if status is None:
            return values
        state = getattr(status.state, "value", status.state)
        values["measurement_state"] = state
        values["measurement_ends_at"] = status.post_window_end
        if state in _DECIDED_STATES:
            values["outcome"] = state
            measurements = await repository.list_measurements(change.change_id)
            latest = measurements[-1] if measurements else None
            if latest is not None:
                values["outcome_reasons"] = tuple(latest.reasons[:3])
                cost = latest.estimate("allocated_cost")
                co2e = latest.estimate("co2e_g")
                if cost is not None:
                    values["cost"] = MeasuredQuantity(cost.annualized, cost.annualized_ci_low, cost.annualized_ci_high)
                if co2e is not None:
                    values["co2e_g"] = MeasuredQuantity(
                        co2e.annualized, co2e.annualized_ci_low, co2e.annualized_ci_high
                    )
        return values

    def _remember(self, plan_id: str, digest: str) -> None:
        self._published[plan_id] = digest
        self._published.move_to_end(plan_id)
        while len(self._published) > self.cache_size:
            self._published.popitem(last=False)


def _primary_record(records: Sequence[PullRequestRecord]) -> PullRequestRecord | None:
    """The plan's PR record (grouped records of one plan share the provider reference)."""
    usable = [record for record in records if record.pr_number is not None and record.provider_id and record.repo]
    return usable[0] if usable else None


def _status(value: object) -> str:
    text = getattr(value, "value", value)
    return str(text).lower() if text is not None else "open"


def _currency(recommendations: Sequence[RecommendationRecord]) -> str:
    for record in recommendations:
        if isinstance(record.impact, dict) and isinstance(record.impact.get("currency"), str):
            return record.impact["currency"]
    return "USD"


def _verification_state(recommendations: Sequence[RecommendationRecord]) -> dict[str, Any]:
    """Interim-verifier fallback when change measurement is off (C0)."""
    applied = [record.applied_at for record in recommendations if record.applied_at is not None]
    values: dict[str, Any] = {}
    if applied:
        values["applied_at"] = min(applied)
    ends = [record.verification_window_end for record in recommendations if record.verification_window_end]
    if ends:
        values["measurement_ends_at"] = max(ends)
    statuses = [record.verification_status for record in recommendations if record.verification_status]
    if statuses and all(status in _VERIFICATION_OUTCOMES for status in statuses):
        outcomes = {_VERIFICATION_OUTCOMES[status] for status in statuses}
        values["outcome"] = "realized" if outcomes == {"realized"} else sorted(outcomes - {"realized"})[0]
        cost = [record.measured_cost_saved for record in recommendations if record.measured_cost_saved is not None]
        co2e = [
            record.measured_co2e_saved_grams
            for record in recommendations
            if record.measured_co2e_saved_grams is not None
        ]
        if cost:
            values["cost"] = MeasuredQuantity(sum(cost))
        if co2e:
            values["co2e_g"] = MeasuredQuantity(sum(co2e))
    elif statuses and any(status in {"pending", "in_progress"} for status in statuses):
        values["measurement_state"] = "measuring"
    return values


def create_pull_request_feedback_publisher(
    *,
    pull_request_repository: Any,
    plan_repository: Any,
    provider_resolver: Any,
    recommendation_repository: Any | None = None,
    db_manager: Any | None = None,
    config: Any | None = None,
) -> PullRequestFeedbackPublisher | None:
    """Compose the publisher from the runtime settings; ``None`` when lifecycle comments are off."""
    if config is None:
        from greenkube.core.config import get_config

        config = get_config()
    if not getattr(config, "GITOPS_PR_LIFECYCLE_COMMENTS", True):
        return None
    change_repository = None
    if db_manager is not None and getattr(config, "CHANGE_MEASUREMENT_ENABLED", True):
        from greenkube.storage.change_repository import ChangeRepository

        change_repository = ChangeRepository(db_manager)
    newer_advice = None
    if recommendation_repository is not None:
        # Recommendation revisions newer than the plan's pin (E7).
        from greenkube.core.recommendation_revisions import newer_advice_hook

        newer_advice = newer_advice_hook(recommendation_repository)
    return PullRequestFeedbackPublisher(
        pull_request_repository=pull_request_repository,
        plan_repository=plan_repository,
        provider_resolver=provider_resolver,
        recommendation_repository=recommendation_repository,
        change_repository=change_repository,
        settings=PullRequestRenderSettings.from_config(config),
        newer_advice=newer_advice,
        lookback_days=int(getattr(config, "GITOPS_PR_LIFECYCLE_LOOKBACK_DAYS", DEFAULT_FEEDBACK_LOOKBACK_DAYS)),
        # patch and ci delivery (F4) run with read-only credentials: no comment writes.
        read_only=configured_delivery_mode(config) != DELIVERY_DIRECT,
    )
