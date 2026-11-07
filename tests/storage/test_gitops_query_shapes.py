"""Index and query-shape regressions for automation storage (roadmap D15, BUG-57).

The audit (``audit3.py``) ran EXPLAIN on the queue and plan queries and found a sync queue
that no longer matched its partial index, a full scan for branch cleanup, N+1 plan
hydration without LIMIT and an expiry sweep that loaded every live plan.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.migrations import MigrationRunner
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig
from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository
from greenkube.storage.postgres.pull_request_repository import PostgresPullRequestRepository
from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository

_BASE_TIME = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(params=("sqlite", "postgres"), ids=("sqlite", "postgres"))
async def store(request, monkeypatch):
    backend = request.param
    postgres_dsn = None
    schema = None
    if backend == "postgres":
        postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not postgres_dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run live PostgreSQL query-shape tests.")
        schema = f"gk_query_shapes_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", ":memory:")
        monkeypatch.setenv("DB_SCHEMA", "public")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    if backend == "postgres":
        repository = PostgresGitOpsRepository(manager)
        pull_requests = PostgresPullRequestRepository(manager)
    else:
        repository = SQLiteGitOpsRepository(manager)
        pull_requests = SQLitePullRequestRepository(manager)
    await repository.save_provider_config(
        GitOpsProviderConfig(id="provider-github", provider_type="github", status="ready")
    )
    await _insert_recommendations(repository, manager)
    try:
        yield repository, pull_requests, manager, backend
    finally:
        await manager.close()
        if postgres_dsn is not None and schema is not None:
            connection = await asyncpg.connect(postgres_dsn)
            try:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await connection.close()


async def _insert_recommendations(repository, manager) -> None:
    async with repository._transaction() as connection:
        for recommendation_id in (9101, 9102, 9103):
            await repository._execute(
                connection,
                """
                INSERT INTO recommendation_history (id, type, description, created_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    recommendation_id,
                    "rightsizing",
                    f"Recommendation {recommendation_id}",
                    repository._db_datetime(datetime.now(timezone.utc)),
                    "active",
                ),
            )


def _source(workload_name: str = "api", *, source_id: str | None = None) -> WorkloadSource:
    return WorkloadSource(
        id=source_id or f"source-{workload_name}",
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name=workload_name,
        provider_id="provider-github",
        repo_url="https://github.com/acme/platform",
        ref="refs/heads/main",
        revision="rev-123",
        path=f"apps/{workload_name}",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="production",
        controller_namespace="argocd",
        controller_uid="application-uid",
        controller_generation=7,
        source_uid="repository-uid",
        source_generation=3,
        source_revision="rev-123",
        renderer=RendererKind.RAW,
        renderer_config={"kind": "raw"},
        confidence=0.95,
        status=BindingStatus.RESOLVED,
        evidence={"controller": "application"},
        observed_at=_BASE_TIME,
        reason="manifest source",
    )


def _plan(
    plan_id: str,
    *,
    workload_name: str = "api",
    created_offset_minutes: int = 0,
    expires_in_minutes: int = 30,
    recommendation_ids: tuple[int, ...] = (9101, 9102),
    now: datetime = _BASE_TIME,
) -> Plan:
    digest = "sha256:" + uuid4().hex + uuid4().hex
    file_plan = FilePlan(
        plan_id=plan_id,
        files=[
            FileEdit(
                path=f"apps/{workload_name}/deployment.yaml",
                operation="update",
                original_sha="original-sha",
                content="replicas: 2\n",
            ),
            FileEdit(path=f"apps/{workload_name}/values.yaml", operation="create", content="cpu: 100m\n"),
        ],
        diff="diff --git a/deployment.yaml b/deployment.yaml",
        rendered_diff="--- before\n+++ after\n",
        base_revision="base-revision",
        source_revision="source-revision",
        renderer=RendererKind.RAW,
        validation=ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest="sha256:" + "b" * 64,
        ),
        digest=digest,
    )
    created_at = now + timedelta(minutes=created_offset_minutes)
    return Plan(
        plan_id=plan_id,
        recommendation_ids=list(recommendation_ids),
        source=_source(workload_name),
        action=ActionPlanV3(
            action_id=f"action-{plan_id}",
            recommendation_ids=list(recommendation_ids),
            target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="production", name=workload_name),
            operations=[SetContainerResources(container="app", resources=ContainerResources(requests={"cpu": "100m"}))],
            evidence_ref="recommendations",
        ),
        file_plan=file_plan,
        status=PlanStatus.VALIDATED,
        digest=digest,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=expires_in_minutes),
    )


def _count_fetches(repository, monkeypatch) -> list[str]:
    calls: list[str] = []
    original = repository._fetch

    async def counting_fetch(connection, query, params=(), **kwargs):
        calls.append(query)
        return await original(connection, query, params, **kwargs)

    monkeypatch.setattr(repository, "_fetch", counting_fetch)
    return calls


@pytest.mark.asyncio
async def test_list_plans_hydrates_in_constant_queries_and_honours_limit(store, monkeypatch):
    repository, _, _, _ = store
    saved = [await repository.save_plan(_plan(f"plan-{index}", created_offset_minutes=index)) for index in range(6)]
    expected = {plan.plan_id: await repository.get_plan(plan.plan_id) for plan in saved}
    calls = _count_fetches(repository, monkeypatch)

    plans = await repository.list_plans()

    # One row query plus one batched query each for files and recommendation links:
    # the old shape issued three queries per plan (BUG-57).
    assert len(calls) == 3
    assert [plan.plan_id for plan in plans] == [f"plan-{index}" for index in range(6)]
    for plan in plans:
        assert plan == expected[plan.plan_id]
        assert plan.file_plan is not None and len(plan.file_plan.files) == 2
        assert plan.recommendation_ids == [9101, 9102]

    limited = await repository.list_plans(status=PlanStatus.VALIDATED, limit=2)
    assert [plan.plan_id for plan in limited] == ["plan-0", "plan-1"]
    assert [plan.plan_id for plan in await repository.list_plans(source_id="source-api", limit=1)] == ["plan-0"]
    assert await repository.list_plans(status=PlanStatus.APPLIED) == []
    for invalid in (0, -1, True, 10_001):
        with pytest.raises(ValueError):
            await repository.list_plans(limit=invalid)


@pytest.mark.asyncio
async def test_workload_and_recommendation_plan_reads_are_batched(store, monkeypatch):
    repository, _, _, _ = store
    for index in range(3):
        await repository.save_plan(_plan(f"api-{index}", created_offset_minutes=index))
    await repository.save_plan(_plan("web-0", workload_name="web", recommendation_ids=(9103,)))
    assert await repository.set_plan_status("api-0", PlanStatus.SUPERSEDED) is not None
    calls = _count_fetches(repository, monkeypatch)

    records = await repository.list_plan_records_for_workload(
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="api",
        statuses=(PlanStatus.VALIDATED, PlanStatus.SUPERSEDED),
    )
    assert len(calls) == 3
    assert [record.plan.plan_id for record in records] == ["api-0", "api-1", "api-2"]
    assert all(record.updated_at is not None for record in records)
    assert records[0].plan.status is PlanStatus.SUPERSEDED

    active = await repository.list_plans_for_workload(
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="api",
        statuses=(PlanStatus.VALIDATED,),
        limit=1,
    )
    assert [plan.plan_id for plan in active] == ["api-1"]

    calls.clear()
    linked = await repository.list_plans_for_recommendation(9101)
    assert len(calls) == 3
    assert [plan.plan_id for plan in linked] == ["api-0", "api-1", "api-2"]
    assert [plan.plan_id for plan in await repository.list_plans_for_recommendation(9103)] == ["web-0"]
    assert await repository.list_plans_for_recommendation(424242) == []


@pytest.mark.asyncio
async def test_list_expired_plans_returns_only_due_plans_oldest_expiry_first(store):
    repository, _, _, _ = store
    now = _BASE_TIME + timedelta(hours=2)
    await repository.save_plan(_plan("expired-late", created_offset_minutes=10, expires_in_minutes=30))
    await repository.save_plan(_plan("expired-early", created_offset_minutes=20, expires_in_minutes=5))
    await repository.save_plan(_plan("still-valid", created_offset_minutes=0, expires_in_minutes=600))
    await repository.save_plan(_plan("terminal", workload_name="web", expires_in_minutes=1))
    assert await repository.set_plan_status("terminal", PlanStatus.SUPERSEDED) is not None
    live = (PlanStatus.DRAFT, PlanStatus.VALIDATED, PlanStatus.APPROVED)

    due = await repository.list_expired_plans(now=now, statuses=live, limit=10)

    assert [plan.plan_id for plan in due] == ["expired-early", "expired-late"]
    assert due[0].file_plan is not None
    assert [plan.plan_id for plan in await repository.list_expired_plans(now=now, statuses=live, limit=1)] == [
        "expired-early"
    ]
    assert await repository.list_expired_plans(now=_BASE_TIME, statuses=live, limit=10) == []
    assert await repository.list_expired_plans(now=now, statuses=(), limit=10) == []
    with pytest.raises(ValueError):
        await repository.list_expired_plans(now=now.replace(tzinfo=None), statuses=live, limit=10)
    with pytest.raises(ValueError):
        await repository.list_expired_plans(now=now, statuses=live, limit=0)


@pytest.mark.asyncio
async def test_branch_cleanup_completion_is_recorded_on_the_pull_request_row(store):
    repository, pull_requests, manager, _ = store
    for plan_id in ("plan-done", "plan-pending", "plan-legacy"):
        await repository.save_plan(_plan(plan_id, workload_name=plan_id))
    for index, plan_id in enumerate(("plan-done", "plan-pending", "plan-legacy"), start=1):
        await pull_requests.save_pull_request(
            PullRequestRecord(
                recommendation_id=9101,
                provider="github",
                provider_id="provider-github",
                repo="acme/platform",
                head_branch=f"greenkube/plan/{plan_id}",
                pr_number=index,
                plan_id=plan_id,
                status=PullRequestStatus.MERGED,
                merge_commit_sha="a" * 40,
            )
        )
    candidates = await pull_requests.list_branch_cleanup_candidates(limit=10)
    assert [record.plan_id for record in candidates] == ["plan-done", "plan-pending", "plan-legacy"]

    await pull_requests.mark_plan_branch_cleanup_complete(
        "plan-done", provider_id="provider-github", repo="acme/platform", branch_name="greenkube/plan/plan-done"
    )
    # A completion recorded before the 0044 column existed (no dual write) is still honoured.
    async with repository._transaction() as connection:
        await repository._execute(
            connection,
            """
            INSERT INTO automation_branch_cleanup_completed (plan_id, provider_id, repo, branch_name)
            VALUES (?, ?, ?, ?)
            """,
            ("plan-legacy", "provider-github", "acme/platform", "greenkube/plan/plan-legacy"),
        )

    remaining = await pull_requests.list_branch_cleanup_candidates(limit=10)
    assert [record.plan_id for record in remaining] == ["plan-pending"]
    async with manager.connection_scope() as connection:
        rows = await repository._fetch(
            connection,
            "SELECT plan_id, branch_cleanup_completed_at FROM recommendation_pull_requests ORDER BY id",
            (),
        )
    completed = {str(row["plan_id"]): row["branch_cleanup_completed_at"] for row in rows}
    assert completed["plan-done"] is not None
    assert completed["plan-pending"] is None


async def _sqlite_query_plans(manager: DatabaseManager, run) -> dict[str, str]:
    """Run ``run`` while tracing SQL, then EXPLAIN every captured SELECT."""
    assert manager.connection is not None
    statements: list[str] = []
    await manager.connection.set_trace_callback(statements.append)
    try:
        await run()
    finally:
        # sqlite3 accepts None to remove the callback; aiosqlite's stub only types callables.
        await manager.connection.set_trace_callback(cast(Any, None))
    plans: dict[str, str] = {}
    for statement in statements:
        normalized = " ".join(statement.split())
        if not normalized.upper().startswith("SELECT"):
            continue
        cursor = await manager.connection.execute("EXPLAIN QUERY PLAN " + normalized)
        plans[normalized] = " | ".join(str(row[3]) for row in await cursor.fetchall())
    return plans


def _plan_for(plans: dict[str, str], fragment: str) -> str:
    matches = [plan for statement, plan in plans.items() if fragment in statement]
    assert matches, f"no traced statement contains {fragment!r}: {list(plans)}"
    return matches[0]


@pytest.mark.asyncio
async def test_sqlite_automation_queue_and_plan_queries_use_their_indexes(store):
    repository, pull_requests, manager, backend = store
    if backend != "sqlite":
        pytest.skip("SQLite EXPLAIN QUERY PLAN check; PostgreSQL is covered separately.")

    async def run() -> None:
        await pull_requests.list_sync_pending_pull_requests(limit=50)
        await pull_requests.list_branch_cleanup_candidates(limit=50)
        await repository.list_plans(status=PlanStatus.DRAFT, limit=10)
        await repository.list_plans(source_id="source-api")
        await repository.list_plan_records_for_workload(
            cluster="cluster-a",
            namespace="production",
            workload_kind="Deployment",
            workload_name="api",
            statuses=(PlanStatus.VALIDATED,),
        )
        await repository.list_expired_plans(now=_BASE_TIME, statuses=(PlanStatus.VALIDATED,), limit=10)

    plans = await _sqlite_query_plans(manager, run)

    assert "idx_reco_pr_sync_queue_v2" in _plan_for(plans, "sync_error_code = 'sync_timeout'")
    cleanup = _plan_for(plans, "automation_branch_cleanup_completed cleanup")
    assert "idx_reco_pr_branch_cleanup_pending" in cleanup
    assert "SCAN pr |" not in cleanup + " |"
    assert "idx_automation_plans_status_created" in _plan_for(plans, "WHERE status = 'draft'")
    assert "idx_automation_plans_source_created" in _plan_for(plans, "WHERE source_id = 'source-api'")
    assert "idx_automation_plans_workload" in _plan_for(plans, "json_extract(source_snapshot, '$.workload_name')")
    assert "idx_automation_plans_status_expiry" in _plan_for(plans, "ORDER BY expires_at")
    for statement, plan in plans.items():
        assert "SCAN automation_plans" not in plan, statement
        assert "SCAN recommendation_pull_requests" not in plan, statement


@pytest.mark.asyncio
async def test_postgres_automation_queue_and_plan_queries_can_use_their_indexes(store, monkeypatch):
    repository, pull_requests, manager, backend = store
    if backend != "postgres":
        pytest.skip("PostgreSQL EXPLAIN check; SQLite is covered separately.")
    captured: list[tuple[str, tuple[Any, ...]]] = []
    original_fetch = asyncpg.connection.Connection.fetch

    async def capturing_fetch(self, query, *args, **kwargs):
        captured.append((query, args))
        return await original_fetch(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.connection.Connection, "fetch", capturing_fetch)
    await pull_requests.list_sync_pending_pull_requests(limit=50)
    await pull_requests.list_branch_cleanup_candidates(limit=50)
    await repository.list_plans(status=PlanStatus.DRAFT, limit=10)
    await repository.list_plans(source_id="source-api")
    await repository.list_plan_records_for_workload(
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="api",
        statuses=(PlanStatus.VALIDATED,),
    )
    monkeypatch.undo()

    expected = {
        "sync_error_code = 'sync_timeout'": "idx_reco_pr_sync_queue_v2",
        "automation_branch_cleanup_completed cleanup": "idx_reco_pr_branch_cleanup_pending",
        "WHERE status = $1": "idx_automation_plans_status_created",
        "WHERE source_id = $1": "idx_automation_plans_source_created",
        "source_snapshot->>'workload_name'": "idx_automation_plans_workload",
    }
    async with manager.connection_scope() as connection:
        async with connection.transaction():
            # Without statistics an empty table is cheapest to scan; disabling sequential scans
            # proves the planner *can* match each partial or expression index to its query.
            await connection.execute("SET LOCAL enable_seqscan = off")
            for fragment, index_name in expected.items():
                matches = [(query, args) for query, args in captured if fragment in query]
                assert matches, f"no captured query contains {fragment!r}"
                query, args = matches[0]
                rows = await connection.fetch("EXPLAIN (FORMAT JSON) " + query, *args)
                plan = json.dumps(json.loads(rows[0][0]) if isinstance(rows[0][0], str) else rows[0][0])
                assert index_name in plan, f"{fragment}: {plan}"


@pytest.mark.asyncio
async def test_migration_0044_backfills_branch_cleanup_completion(tmp_path):
    """Upgrade path: completions recorded by 0036 are copied onto their pull-request rows."""
    database = str(tmp_path / "upgrade.db")
    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(database) as connection:
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version >= 44:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.execute("INSERT INTO automation_plans (plan_id) VALUES ('plan-done'), ('plan-open')")
        await connection.execute(
            """
            INSERT INTO recommendation_pull_requests
                (recommendation_id, provider, repo, head_branch, plan_id, provider_id, status)
            VALUES (1, 'github', 'acme/platform', 'greenkube/plan/plan-done', 'plan-done', 'gh', 'merged'),
                   (2, 'github', 'acme/platform', 'greenkube/plan/plan-open', 'plan-open', 'gh', 'merged')
            """
        )
        await connection.execute(
            """
            INSERT INTO automation_branch_cleanup_completed (plan_id, provider_id, repo, branch_name)
            VALUES ('plan-done', 'gh', 'acme/platform', 'greenkube/plan/plan-done')
            """
        )
        await connection.commit()

    manager = DatabaseManager()
    try:
        await manager.setup_sqlite(db_path=database)
    finally:
        await manager.close()

    async with aiosqlite.connect(database) as connection:
        cursor = await connection.execute(
            "SELECT plan_id, branch_cleanup_completed_at FROM recommendation_pull_requests ORDER BY id"
        )
        rows = {plan_id: completed_at for plan_id, completed_at in await cursor.fetchall()}
        cursor = await connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        indexes = {row[0] for row in await cursor.fetchall()}
    assert rows["plan-done"] is not None
    assert rows["plan-open"] is None
    assert "idx_reco_pr_sync_queue" not in indexes
    assert {
        "idx_reco_pr_sync_queue_v2",
        "idx_reco_pr_branch_cleanup_pending",
        "idx_automation_plans_status_created",
        "idx_automation_plans_source_created",
        "idx_automation_plans_workload",
    } <= indexes
