""" "Newer advice available" reaches an open plan PR before the merge (roadmap J5, E7).

Integration: the real SQLite recommendation repository queues the new
revision behind the open plan, the real ``newer_advice_hook`` reads the plan
pins, and the publisher refreshes the open PR's status comment, rate limited
and idempotent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from greenkube.automation.pr_content.model import PullRequestRenderSettings
from greenkube.automation.pr_feedback import FeedbackOutcome, PullRequestFeedbackPublisher
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.recommendation_revisions import newer_advice_hook
from greenkube.gitops.contracts import PlanStatus
from greenkube.models.metrics import PullRequestStatus, RecommendationRecord, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]
from tests.automation.test_pr_feedback import (  # pyrefly: ignore[missing-import]
    NOW,
    CountingProvider,
    Plans,
    PullRequests,
    Resolver,
    _record,
)


class OpenPullRequests(PullRequests):
    """The PR repository fake with the poller's open-PR listing."""

    def __init__(self, records) -> None:
        super().__init__(records)
        self.open_listings = 0

    async def list_pollable_pull_requests(self, *, limit, after_id=None):
        self.open_listings += 1
        return [
            record
            for record in self.records
            if record.status in (PullRequestStatus.OPEN, PullRequestStatus.PENDING)
            and (after_id is None or (record.id or 0) > after_id)
        ][:limit]


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "feedback.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


def _cpu(cpu: int) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="payments",
        container_name="api",
        type=RecommendationType.RIGHTSIZING_CPU,
        description=f"cpu {cpu}m",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=cpu,
        fingerprint="fp-cpu",
    )


async def _open_plan(repo: SQLiteRecommendationRepository, plan_id: str) -> RecommendationRecord:
    await repo.upsert_recommendations([_cpu(300)])
    [record] = await repo.get_active_recommendations()
    assert record.id is not None
    await repo.pin_plan_revisions(plan_id, {record.id: record.revision})
    async with repo.db_manager.connection_scope() as conn:
        await conn.execute("UPDATE recommendation_history SET action_status = 'pr_open' WHERE id = ?", (record.id,))
        await conn.commit()
    return record


def _publisher(repo, record: RecommendationRecord, clock, **kwargs: Any):
    provider = CountingProvider()
    pull_requests = OpenPullRequests(
        [
            _record(
                1,
                record.id,
                plan_id="pln-1",
                status=PullRequestStatus.OPEN,
                merge_commit_sha=None,
                merged_at=None,
            )
        ]
    )
    publisher = PullRequestFeedbackPublisher(
        pull_request_repository=pull_requests,
        plan_repository=Plans(fx.plan(recommendation_ids=(record.id,), status=PlanStatus.APPROVED)),
        provider_resolver=Resolver(provider),
        recommendation_repository=repo,
        settings=PullRequestRenderSettings(external_url="https://greenkube.example.com"),
        newer_advice=newer_advice_hook(repo),
        clock=clock,
        **kwargs,
    )
    return publisher, provider, pull_requests


async def test_open_pr_without_newer_advice_gets_no_comment(repo):
    record = await _open_plan(repo, "pln-1")
    publisher, provider, _ = _publisher(repo, record, lambda: NOW)

    summary = await publisher.reconcile_once()

    assert summary.plans == 1 and summary.skipped == 1
    assert provider.calls == 0 and provider.comments == {}


async def test_newer_advice_appears_on_the_open_pr_once_and_is_rate_limited(repo):
    record = await _open_plan(repo, "pln-1")
    # A later scan computes different advice: queued behind the open plan (E7).
    await repo.upsert_recommendations([_cpu(220)])
    now = [NOW]
    publisher, provider, pull_requests = _publisher(repo, record, lambda: now[0])

    summary = await publisher.reconcile_once()
    assert summary.created == 1
    [body] = provider.comments[17]
    assert "| Merged | ⏳ | Waiting for review and merge. |" in body
    assert "> **Newer advice available:**" in body
    assert f"recommendation #{record.id} is at revision 2 (this change implements revision 1)" in body

    # Rate limited: the open plan is not checked again before the interval.
    summary = await publisher.reconcile_once()
    assert summary.plans == 0 and provider.calls == 1

    # Idempotent: after the interval the same body costs no provider call.
    now[0] = NOW + timedelta(minutes=16)
    summary = await publisher.reconcile_once()
    assert (summary.plans, summary.unchanged) == (1, 1)
    assert provider.calls == 1 and len(provider.comments[17]) == 1
    assert pull_requests.open_listings == 3


async def test_publish_of_an_open_pr_reports_skipped_without_newer_advice(repo):
    record = await _open_plan(repo, "pln-1")
    publisher, provider, _ = _publisher(repo, record, lambda: NOW)
    assert await publisher.publish("pln-1") is FeedbackOutcome.SKIPPED
    assert provider.calls == 0


async def test_open_prs_are_not_listed_without_the_newer_advice_hook(repo):
    record = await _open_plan(repo, "pln-1")
    await repo.upsert_recommendations([_cpu(220)])
    publisher, provider, pull_requests = _publisher(repo, record, lambda: NOW)
    publisher.newer_advice = None

    summary = await publisher.reconcile_once()

    assert summary.plans == 0 and pull_requests.open_listings == 0 and provider.calls == 0
