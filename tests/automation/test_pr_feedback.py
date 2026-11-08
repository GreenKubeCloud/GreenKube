"""One GreenKube status comment per merged PR, updated at each lifecycle step (roadmap J5)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from greenkube.automation.git.base import CommentUpsertResult, GitProviderCapability, GitRepository
from greenkube.automation.pr_content import LIFECYCLE_MARKER, PullRequestRenderSettings
from greenkube.automation.pr_feedback import (
    FeedbackOutcome,
    PullRequestFeedbackPublisher,
    create_pull_request_feedback_publisher,
)
from greenkube.automation.sync_runtime import GitOpsSyncRuntime
from greenkube.core.config import Config
from greenkube.gitops.contracts import PlanStatus
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus, RecommendationRecord
from greenkube.verify.model import (
    ChangeEvent,
    ChangeMeasurement,
    ChangeOutcome,
    ChangeStatus,
    MeasurementState,
    SavingsEstimate,
    TimeWindow,
    WindowPlan,
    WorkloadRef,
)
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]

NOW = datetime.now(timezone.utc).replace(microsecond=0)
MERGE_SHA = "b" * 40


class CountingProvider:
    """A provider fake that stores comments and counts every API call."""

    name = "github"

    def __init__(self, *, comments_supported: bool = True) -> None:
        self.capabilities = (
            frozenset({GitProviderCapability.PULL_REQUEST_COMMENTS}) if comments_supported else frozenset()
        )
        self.comments: dict[int, list[str]] = {}
        self.calls = 0
        self.closed = 0

    def supports(self, capability) -> bool:
        return capability in self.capabilities

    async def upsert_pull_request_comment(self, repo, number, *, marker, body):
        self.calls += 1
        comments = self.comments.setdefault(number, [])
        for index, existing in enumerate(comments):
            if existing.startswith(marker):
                if existing == body:
                    return CommentUpsertResult.UNCHANGED, index
                comments[index] = body
                return CommentUpsertResult.UPDATED, index
        comments.append(body)
        return CommentUpsertResult.CREATED, len(comments) - 1

    async def close(self) -> None:
        self.closed += 1


class Resolver:
    def __init__(self, provider: CountingProvider) -> None:
        self.provider = provider
        self.calls = 0

    async def resolve(self, source):
        self.calls += 1
        return type(
            "Target",
            (),
            {"provider": self.provider, "repository": GitRepository(provider="github", owner="acme", name="gitops")},
        )()


class PullRequests:
    def __init__(self, records: list[PullRequestRecord]) -> None:
        self.records = records
        self.listings: list[dict[str, Any]] = []

    async def list_pull_requests_by_plan_id(self, plan_id: str):
        return [record for record in self.records if record.plan_id == plan_id]

    async def list_feedback_pull_requests(self, *, merged_since, limit, after_id=None):
        self.listings.append({"merged_since": merged_since, "limit": limit, "after_id": after_id})
        return [
            record
            for record in self.records
            if record.status is PullRequestStatus.MERGED
            and record.merged_at is not None
            and record.merged_at >= merged_since
            and (after_id is None or (record.id or 0) > after_id)
        ][:limit]


class Plans:
    def __init__(self, *plans) -> None:
        self.plans = {plan.plan_id: plan for plan in plans}

    async def get_plan(self, plan_id: str):
        return self.plans.get(plan_id)


class Recommendations:
    def __init__(self, *records: RecommendationRecord) -> None:
        self.records = {record.id: record for record in records}

    async def get_recommendation_by_id(self, rec_id: int):
        return self.records.get(rec_id)


class Changes:
    def __init__(self) -> None:
        self.change: ChangeEvent | None = None
        self.status: ChangeStatus | None = None
        self.measurements: list[ChangeMeasurement] = []

    async def change_ids_for_recommendations(self, recommendation_ids):
        if self.change is None:
            return {}
        return {item: self.change.change_id for item in recommendation_ids}

    async def get_change(self, change_id):
        return self.change if self.change is not None and self.change.change_id == change_id else None

    async def get_status(self, change_id):
        return self.status

    async def list_measurements(self, change_id):
        return list(self.measurements)


def _record(record_id: int = 1, recommendation_id: int = 41, **updates: Any) -> PullRequestRecord:
    values: dict[str, Any] = {
        "id": record_id,
        "recommendation_id": recommendation_id,
        "provider": "github",
        "provider_id": "github-main",
        "repo": "acme/gitops",
        "pr_number": 17,
        "plan_id": "pln-1",
        "status": PullRequestStatus.MERGED,
        "merge_commit_sha": MERGE_SHA,
        "merged_at": NOW - timedelta(hours=2),
    }
    values.update(updates)
    return PullRequestRecord(**values)


def _publisher(records=None, *, changes=None, recommendations=None, provider=None, **kwargs):
    provider = provider or CountingProvider()
    pull_requests = PullRequests(records if records is not None else [_record(1, 41), _record(2, 42)])
    publisher = PullRequestFeedbackPublisher(
        pull_request_repository=pull_requests,
        plan_repository=Plans(fx.plan(status=PlanStatus.MERGED)),
        provider_resolver=Resolver(provider),
        recommendation_repository=Recommendations(*(recommendations or [fx.recommendation(41), fx.recommendation(42)])),
        change_repository=changes,
        settings=PullRequestRenderSettings(external_url="https://greenkube.example.com"),
        **kwargs,
    )
    return publisher, provider, pull_requests


def _change(**updates: Any) -> ChangeEvent:
    values: dict[str, Any] = {
        "change_id": "chg-1",
        "recommendation_ids": [41, 42],
        "plan_id": "pln-1",
        "target": WorkloadRef(namespace="payments", kind="Deployment", name="api"),
        "rollout_started_at": NOW - timedelta(hours=1, minutes=5),
        "rollout_completed_at": NOW - timedelta(hours=1),
        "baseline_window_end": NOW - timedelta(hours=2),
    }
    values.update(updates)
    return ChangeEvent(**values)


def _measurement(outcome: ChangeOutcome) -> ChangeMeasurement:
    window = TimeWindow(start=NOW - timedelta(days=7), end=NOW)
    return ChangeMeasurement(
        measurement_id="msr-1",
        change_id="chg-1",
        strategy_key="request_rightsizing",
        strategy_version="1.0",
        windows=WindowPlan(pre=window, post=window, warmup_hours=1, min_coverage=0.8),
        estimates=[
            SavingsEstimate(
                quantity="allocated_cost",
                unit="currency",
                claimed=True,
                annualized=380.0,
                annualized_ci_low=310.0,
                annualized_ci_high=450.0,
                outcome=outcome,
            ),
            SavingsEstimate(quantity="co2e_g", unit="gCO2e", claimed=False, annualized=9_000.0, outcome=outcome),
        ],
        outcome=outcome,
        reasons=["CI lower bound above the noise floor"],
    )


@pytest.mark.asyncio
async def test_merged_pr_gets_one_comment_and_unchanged_state_costs_no_provider_call():
    publisher, provider, _ = _publisher()
    resolver = publisher.provider_resolver
    assert isinstance(resolver, Resolver)

    assert await publisher.publish("pln-1") is FeedbackOutcome.CREATED
    assert await publisher.publish("pln-1") is FeedbackOutcome.UNCHANGED

    assert provider.calls == 1
    assert resolver.calls == 1
    assert len(provider.comments[17]) == 1
    body = provider.comments[17][0]
    assert body.startswith(LIFECYCLE_MARKER)
    assert f"Merge commit `{MERGE_SHA[:12]}`" in body
    assert "Waiting for Argo CD `argocd/payments-prod` to sync the merge." in body
    assert "https://greenkube.example.com/changes/pln-1" in body


@pytest.mark.asyncio
async def test_each_lifecycle_step_edits_the_same_comment():
    changes = Changes()
    records = [_record(1, 41), _record(2, 42)]
    publisher, provider, pull_requests = _publisher(records, changes=changes)
    assert await publisher.publish("pln-1") is FeedbackOutcome.CREATED

    synced = {"sync_status": "synced", "sync_revision": MERGE_SHA, "sync_observed_at": NOW - timedelta(minutes=90)}
    pull_requests.records = [record.model_copy(update=synced) for record in records]
    assert await publisher.publish("pln-1") is FeedbackOutcome.UPDATED
    assert f"Revision `{MERGE_SHA[:12]}` by Argo CD `argocd/payments-prod`" in provider.comments[17][0]
    assert "Waiting for the rollout to complete." in provider.comments[17][0]

    changes.change = _change()
    changes.status = ChangeStatus(
        change_id="chg-1", state=MeasurementState.MEASURING, post_window_end=NOW + timedelta(days=7)
    )
    assert await publisher.publish("pln-1") is FeedbackOutcome.UPDATED
    body = provider.comments[17][0]
    assert "Rollout completed at" in body
    assert "Measuring savings. Results expected around" in body

    changes.status = changes.status.model_copy(update={"state": MeasurementState.REALIZED})
    changes.measurements = [_measurement(ChangeOutcome.REALIZED)]
    assert await publisher.publish("pln-1") is FeedbackOutcome.UPDATED
    body = provider.comments[17][0]
    assert "**Outcome: Realized.** Measured saving: cost $380.00/yr (90% CI $310.00 to $450.00)" in body
    assert "CO₂e 9.0 kgCO₂e/yr (reported)" in body
    assert "https://greenkube.example.com/ledger?change=chg-1" in body

    assert len(provider.comments[17]) == 1
    assert provider.calls == 4


@pytest.mark.asyncio
async def test_rollback_review_and_newer_advice_are_reported():
    async def newer_advice(plan, recommendations):
        return "recommendation #41 has a newer revision (cpu 250m)."

    recommendations = [fx.recommendation(41, status="rollback_review"), fx.recommendation(42)]
    publisher, provider, _ = _publisher(recommendations=recommendations, newer_advice=newer_advice)
    await publisher.publish("pln-1")
    body = provider.comments[17][0]
    assert "| Outcome | ❌ | Rollback review |" in body
    assert "**Rollback review:**" in body
    assert "> **Newer advice available:** recommendation #41 has a newer revision (cpu 250m)." in body


@pytest.mark.asyncio
async def test_interim_verifier_fields_are_used_without_change_measurement():
    applied_at = NOW - timedelta(days=4)
    recommendations = [
        fx.recommendation(
            41,
            applied_at=applied_at,
            verification_status="passed",
            measured_cost_saved=120.0,
            measured_co2e_saved_grams=2_000.0,
        ),
        fx.recommendation(
            42,
            applied_at=applied_at + timedelta(minutes=1),
            verification_status="passed",
            measured_cost_saved=80.0,
        ),
    ]
    publisher, provider, _ = _publisher(recommendations=recommendations)
    await publisher.publish("pln-1")
    body = provider.comments[17][0]
    assert "**Outcome: Realized.** Measured saving: cost $200.00/yr; CO₂e 2.0 kgCO₂e/yr (reported)." in body


@pytest.mark.asyncio
async def test_unsupported_provider_and_unknown_plans_are_skipped():
    provider = CountingProvider(comments_supported=False)
    publisher, _, _ = _publisher(provider=provider)
    assert await publisher.publish("pln-1") is FeedbackOutcome.UNSUPPORTED
    assert await publisher.publish("missing") is FeedbackOutcome.SKIPPED
    assert provider.calls == 0
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_record_for_another_repository_is_never_commented():
    publisher, provider, _ = _publisher([_record(1, 41, repo="other/repo"), _record(2, 42, repo="other/repo")])
    assert await publisher.publish("pln-1") is FeedbackOutcome.SKIPPED
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_reconcile_refreshes_recent_merged_prs_once_per_plan():
    old = _record(3, 43, plan_id="pln-old", merged_at=NOW - timedelta(days=90))
    publisher, provider, pull_requests = _publisher([_record(1, 41), _record(2, 42), old], clock=lambda: NOW)

    summary = await publisher.reconcile_once()
    assert (summary.plans, summary.created, summary.errors) == (1, 1, 0)
    assert pull_requests.listings[0]["merged_since"] == NOW - timedelta(days=30)

    summary = await publisher.reconcile_once()
    assert (summary.created, summary.updated, summary.unchanged) == (0, 0, 1)
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_restarted_publisher_does_not_post_a_second_comment():
    provider = CountingProvider()
    first, _, _ = _publisher(provider=provider)
    await first.publish("pln-1")
    restarted, _, _ = _publisher(provider=provider)
    assert await restarted.publish("pln-1") is FeedbackOutcome.UNCHANGED
    assert len(provider.comments[17]) == 1


def test_factory_honours_the_lifecycle_comment_setting():
    kwargs: dict[str, Any] = {
        "pull_request_repository": object(),
        "plan_repository": object(),
        "provider_resolver": object(),
    }
    assert create_pull_request_feedback_publisher(**kwargs, config=Config(GITOPS_PR_LIFECYCLE_COMMENTS=False)) is None
    publisher = create_pull_request_feedback_publisher(
        **kwargs,
        config=Config(GITOPS_PR_LIFECYCLE_LOOKBACK_DAYS=10, GREENKUBE_EXTERNAL_URL="https://gk.example.com"),
    )
    assert publisher is not None
    assert publisher.lookback_days == 10
    assert publisher.settings.external_url == "https://gk.example.com"


class RecordingFeedback:
    def __init__(self) -> None:
        self.notified: list[str] = []
        self.reconciles = 0

    async def notify(self, plan_id: str) -> None:
        self.notified.append(plan_id)

    async def reconcile_once(self):
        self.reconciles += 1


@pytest.mark.asyncio
async def test_sync_runtime_notifies_the_publisher_when_a_merge_is_synced():
    from tests.automation.test_sync_runtime import (  # pyrefly: ignore[missing-import]
        NOW as SYNC_NOW,
    )
    from tests.automation.test_sync_runtime import (  # pyrefly: ignore[missing-import]
        _CustomObjectsApi,
        _plan,
        _PlanRepository,
        _pr_record,
        _PullRequestRepository,
    )

    feedback = RecordingFeedback()
    runtime = GitOpsSyncRuntime(
        _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)]),
        _PlanRepository(_plan()),
        _CustomObjectsApi(),
        clock=lambda: SYNC_NOW,
        interval_seconds=30,
        sync_timeout_seconds=300,
        feedback=feedback,
    )
    summary = await runtime.poll_once()
    assert summary.synced == 1
    assert feedback.notified == ["plan-1"]


@pytest.mark.asyncio
async def test_sync_loop_reconciles_comments_at_most_once_per_feedback_interval():
    from tests.automation.test_sync_runtime import (  # pyrefly: ignore[missing-import]
        _CustomObjectsApi,
        _plan,
        _PlanRepository,
        _PullRequestRepository,
    )

    feedback = RecordingFeedback()
    clock = {"now": NOW}
    runtime = GitOpsSyncRuntime(
        _PullRequestRepository([]),
        _PlanRepository(_plan()),
        _CustomObjectsApi(),
        clock=lambda: clock["now"],
        interval_seconds=30,
        sync_timeout_seconds=300,
        feedback=feedback,
        feedback_interval_seconds=300,
    )
    await runtime.reconcile_feedback()
    clock["now"] += timedelta(seconds=30)
    await runtime.reconcile_feedback()
    clock["now"] += timedelta(seconds=300)
    await runtime.reconcile_feedback()
    assert feedback.reconciles == 2

    stop = asyncio.Event()
    stop.set()
    await runtime.run(stop)
    assert feedback.reconciles == 2
