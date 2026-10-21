from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE
from greenkube.storage.postgres.pull_request_repository import PostgresPullRequestRepository
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository


@pytest.fixture
async def sqlite_repository():
    manager = DatabaseManager()
    await manager.setup_sqlite(db_path=":memory:")
    try:
        yield SQLitePullRequestRepository(manager)
    finally:
        await manager.close()


def _record(
    *,
    recommendation_id: int,
    status: PullRequestStatus = PullRequestStatus.OPEN,
    provider_id: str | None = "github-main",
    plan_id: str | None = "plan-1",
    pr_number: int | None = 42,
) -> PullRequestRecord:
    return PullRequestRecord(
        recommendation_id=recommendation_id,
        provider="github",
        provider_id=provider_id,
        repo="acme/platform",
        pr_number=pr_number,
        plan_id=plan_id,
        status=status,
    )


@pytest.mark.asyncio
async def test_sqlite_poll_query_returns_bounded_plan_linked_open_pages(sqlite_repository):
    first = await sqlite_repository.save_pull_request(_record(recommendation_id=1))
    await sqlite_repository.save_pull_request(_record(recommendation_id=2, status=PullRequestStatus.CLOSED))
    await sqlite_repository.save_pull_request(_record(recommendation_id=3, provider_id=None))
    await sqlite_repository.save_pull_request(_record(recommendation_id=4, plan_id=None))
    second = await sqlite_repository.save_pull_request(
        _record(recommendation_id=5, status=PullRequestStatus.PENDING, pr_number=43)
    )
    await sqlite_repository.save_pull_request(_record(recommendation_id=6, pr_number=None))

    first_page = await sqlite_repository.list_pollable_pull_requests(limit=1)
    second_page = await sqlite_repository.list_pollable_pull_requests(limit=1, after_id=first.id)
    end_page = await sqlite_repository.list_pollable_pull_requests(limit=1, after_id=second.id)

    assert first_page == [first]
    assert second_page == [second]
    assert end_page == []


@pytest.mark.asyncio
async def test_sqlite_poll_query_rejects_unbounded_page_requests(sqlite_repository):
    with pytest.raises(ValueError):
        await sqlite_repository.list_pollable_pull_requests(limit=MAX_PULL_REQUEST_POLL_BATCH_SIZE + 1)
    with pytest.raises(ValueError):
        await sqlite_repository.list_pollable_pull_requests(limit=1, after_id=-1)


@pytest.mark.asyncio
async def test_postgres_poll_query_uses_same_link_filters_and_page_bounds():
    row = {
        "id": 5,
        "recommendation_id": 5,
        "provider": "github",
        "repo": "acme/platform",
        "base_branch": "main",
        "head_branch": "green-kube/recommendation-5",
        "pr_number": 43,
        "pr_url": None,
        "status": "open",
        "error": None,
        "plan_id": "plan-5",
        "provider_id": "github-main",
        "commit_sha": None,
        "merge_commit_sha": None,
        "merged_at": None,
        "closed_at": None,
        "sync_status": None,
        "sync_revision": None,
        "sync_observed_at": None,
        "sync_error_code": None,
        "created_at": datetime(2026, 10, 21, tzinfo=timezone.utc),
        "updated_at": None,
    }

    class FakeConnection:
        query = ""
        params: tuple[object, ...] = ()

        async def fetch(self, query: str, *params: object):
            self.query = query
            self.params = params
            return [row]

    connection = FakeConnection()

    class FakeDatabaseManager:
        @asynccontextmanager
        async def connection_scope(self):
            yield connection

    repository = PostgresPullRequestRepository(FakeDatabaseManager())

    records = await repository.list_pollable_pull_requests(limit=7, after_id=4)

    assert len(records) == 1
    assert records[0].id == 5
    assert connection.params == (4, 7)
    assert "status IN ('pending', 'open')" in connection.query
    assert "plan_id IS NOT NULL" in connection.query
    assert "provider_id IS NOT NULL" in connection.query
    assert "pr_number IS NOT NULL" in connection.query
    assert "id > $1" in connection.query
    assert "LIMIT $2" in connection.query
