"""Regression tests for SQLite connection poisoning (roadmap D8, BUG-50)."""

import re
from pathlib import Path

import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_pull_request_repository import PullRequestStateConflictError
from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
from greenkube.storage.sqlite.plan_operation_repository import SQLitePlanOperationRepository
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.unit_of_work import UnitOfWork

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "greenkube"
# Only these modules may issue a raw BEGIN; every other SQLite write goes through a
# context manager that always commits or rolls back.
_RAW_BEGIN_ALLOWLIST = {
    "core/db.py",
    "core/migrations/runner.py",
    "storage/unit_of_work.py",
    "storage/base_plan_operation_sql_repository.py",
}


@pytest.fixture
async def sqlite_manager(monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("DB_SCHEMA", "public")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()


async def _assert_connection_is_writable(manager: DatabaseManager) -> None:
    gitops = SQLiteGitOpsRepository(manager)
    operations = SQLitePlanOperationRepository(manager)
    assert manager.connection is not None
    assert not manager.connection.in_transaction
    lease = await gitops.try_acquire_pr_polling_lease("owner-after-conflict", lease_seconds=30)
    assert lease is not None
    assert await operations.claim_next("worker-after-conflict", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_pull_request_compare_and_set_conflict_does_not_poison_the_connection(sqlite_manager):
    """Port of the audit1.py reproduction: a CAS conflict left the shared connection mid-transaction."""
    repository = SQLitePullRequestRepository(sqlite_manager)
    saved = await repository.save_pull_request(
        PullRequestRecord(recommendation_id=1, provider="github", repo="acme/api", status=PullRequestStatus.PENDING)
    )
    assert saved.id is not None

    with pytest.raises(PullRequestStateConflictError):
        await repository.update_pull_request(saved.id, {"status": "open"}, expected_statuses=("merged",))

    await _assert_connection_is_writable(sqlite_manager)
    current = await repository.get_pull_request_by_id(saved.id)
    assert current is not None
    assert current.status is PullRequestStatus.PENDING


@pytest.mark.asyncio
async def test_missing_pull_request_update_does_not_poison_the_connection(sqlite_manager):
    repository = SQLitePullRequestRepository(sqlite_manager)
    with pytest.raises(ValueError, match="not found"):
        await repository.update_pull_request(999, {"status": "open"}, expected_statuses=("pending",))
    await _assert_connection_is_writable(sqlite_manager)


@pytest.mark.asyncio
async def test_connection_scope_rolls_back_an_uncommitted_write_on_error(sqlite_manager):
    with pytest.raises(RuntimeError, match="boom"):
        async with sqlite_manager.connection_scope() as connection:
            await connection.execute(
                "INSERT INTO recommendation_pull_requests (recommendation_id, provider, repo, status) "
                "VALUES (7, 'github', 'acme/api', 'pending')"
            )
            raise RuntimeError("boom")

    await _assert_connection_is_writable(sqlite_manager)
    async with sqlite_manager.connection_scope() as connection:
        cursor = await connection.execute("SELECT COUNT(*) FROM recommendation_pull_requests")
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_leaked_transaction_is_discarded_before_the_next_unit_of_work(sqlite_manager):
    async with sqlite_manager.connection_scope() as connection:
        # A buggy caller performs DML and returns without committing.
        await connection.execute(
            "INSERT INTO recommendation_pull_requests (recommendation_id, provider, repo, status) "
            "VALUES (8, 'github', 'acme/api', 'pending')"
        )
    assert sqlite_manager.connection is not None
    assert sqlite_manager.connection.in_transaction

    async with UnitOfWork(sqlite_manager) as unit_of_work:
        assert unit_of_work.connection is not None
        await unit_of_work.connection.execute(
            "INSERT INTO recommendation_pull_requests (recommendation_id, provider, repo, status) "
            "VALUES (9, 'github', 'acme/api', 'pending')"
        )

    async with sqlite_manager.connection_scope() as connection:
        cursor = await connection.execute("SELECT recommendation_id FROM recommendation_pull_requests ORDER BY id")
        assert [row[0] for row in await cursor.fetchall()] == [9]


@pytest.mark.asyncio
async def test_transaction_commits_on_success_and_rolls_back_on_error(sqlite_manager):
    async with sqlite_manager.transaction() as connection:
        await connection.execute(
            "INSERT INTO recommendation_pull_requests (recommendation_id, provider, repo, status) "
            "VALUES (10, 'github', 'acme/api', 'pending')"
        )
    with pytest.raises(RuntimeError):
        async with sqlite_manager.transaction() as connection:
            await connection.execute(
                "INSERT INTO recommendation_pull_requests (recommendation_id, provider, repo, status) "
                "VALUES (11, 'github', 'acme/api', 'pending')"
            )
            raise RuntimeError("rollback")

    assert sqlite_manager.connection is not None
    assert not sqlite_manager.connection.in_transaction
    async with sqlite_manager.connection_scope() as connection:
        cursor = await connection.execute("SELECT recommendation_id FROM recommendation_pull_requests ORDER BY id")
        assert [row[0] for row in await cursor.fetchall()] == [10]


@pytest.mark.asyncio
async def test_nested_transaction_uses_a_savepoint_inside_a_unit_of_work(sqlite_manager):
    async with UnitOfWork(sqlite_manager) as unit_of_work:
        assert unit_of_work.connection is not None
        await unit_of_work.connection.execute(
            "INSERT INTO recommendation_pull_requests (recommendation_id, provider, repo, status) "
            "VALUES (12, 'github', 'acme/api', 'pending')"
        )
        with pytest.raises(RuntimeError):
            async with sqlite_manager.transaction() as connection:
                await connection.execute(
                    "INSERT INTO recommendation_pull_requests (recommendation_id, provider, repo, status) "
                    "VALUES (13, 'github', 'acme/api', 'pending')"
                )
                raise RuntimeError("inner failure")
        assert unit_of_work.connection.in_transaction

    async with sqlite_manager.connection_scope() as connection:
        cursor = await connection.execute("SELECT recommendation_id FROM recommendation_pull_requests ORDER BY id")
        assert [row[0] for row in await cursor.fetchall()] == [12]


def test_no_bare_begin_statements_outside_transaction_helpers():
    pattern = re.compile(r"""execute\(\s*f?["'](?:BEGIN|begin)\b""")
    offenders = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        if relative in _RAW_BEGIN_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text) or re.search(r"""["']BEGIN(?: IMMEDIATE| EXCLUSIVE| DEFERRED)?["']""", text):
            offenders.append(relative)
    assert offenders == [], f"Use DatabaseManager.transaction() instead of a bare BEGIN in: {offenders}"
