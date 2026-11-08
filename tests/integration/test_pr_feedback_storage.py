"""Merged plan PRs listed for lifecycle comments (roadmap J5) on SQLite and PostgreSQL."""

import os
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.postgres.pull_request_repository import PostgresPullRequestRepository
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[DatabaseManager, None]:
    backend = request.param
    postgres_dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if backend == "postgres":
        postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not postgres_dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run real PostgreSQL integration tests.")
        schema = f"gk_test_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "greenkube.sqlite"))
        monkeypatch.setenv("DB_SCHEMA", "public")

    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if postgres_dsn and schema:
            conn = await asyncpg.connect(postgres_dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _repository(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresPullRequestRepository(manager)
    return SQLitePullRequestRepository(manager)


def _record(recommendation_id: int, **updates) -> PullRequestRecord:
    values = {
        "recommendation_id": recommendation_id,
        "provider": "github",
        "provider_id": "github-main",
        "repo": "acme/gitops",
        "pr_number": recommendation_id,
        "plan_id": f"pln-{recommendation_id}",
        "status": PullRequestStatus.MERGED,
        "merge_commit_sha": "a" * 40,
        "merged_at": NOW - timedelta(days=1),
        "head_branch": f"greenkube/plan/{recommendation_id:032x}",
    }
    values.update(updates)
    return PullRequestRecord(**values)


@pytest.mark.asyncio
async def test_feedback_listing_returns_recent_merged_plan_prs_in_pages(database):
    repository = _repository(database)
    recent = [await repository.save_pull_request(_record(item)) for item in (1, 2, 3)]
    await repository.save_pull_request(_record(4, merged_at=NOW - timedelta(days=45)))
    await repository.save_pull_request(_record(5, status=PullRequestStatus.OPEN, merged_at=None))
    await repository.save_pull_request(_record(6, status=PullRequestStatus.CLOSED, merged_at=None))
    await repository.save_pull_request(_record(7, plan_id=None))
    await repository.save_pull_request(_record(8, pr_number=None))
    # Synced PRs stay listed: their comment still changes when the outcome is measured.
    synced = await repository.save_pull_request(_record(9, sync_status="synced"))

    since = NOW - timedelta(days=30)
    first = await repository.list_feedback_pull_requests(merged_since=since, limit=2)
    assert [record.id for record in first] == [recent[0].id, recent[1].id]
    rest = await repository.list_feedback_pull_requests(merged_since=since, limit=100, after_id=first[-1].id)
    assert [record.id for record in rest] == [recent[2].id, synced.id]
    assert all(record.status is PullRequestStatus.MERGED for record in first + rest)


@pytest.mark.asyncio
async def test_feedback_listing_validates_its_page(database):
    repository = _repository(database)
    with pytest.raises(ValueError):
        await repository.list_feedback_pull_requests(merged_since=NOW, limit=0)
