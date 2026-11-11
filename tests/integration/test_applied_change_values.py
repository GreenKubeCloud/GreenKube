"""Applied change values through the real repositories (bug 1: before == after).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL. It
replays what the Minikube loop did to a 400m -> 10m rightsizing:

* while its pull request is open (and after the merge, until the change is
  observed), the recommendation is ``pr_open``, so a scan can neither rewrite
  its baseline and proposal nor insert a duplicate;
* once applied, a scan that regenerates the recommendation from the lagging
  hourly rollup (current = the replaced 400m) never rewrites the ``actual_*``
  value detected after the change;
* a pull request closed unmerged returns the recommendation to ``active``;
* a scan within the rollup lag after the change never re-offers it;
* migration 0061 clears the ``actual_*`` values the old rescans reverted.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.gitops.contracts import ActionStatus
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[DatabaseManager, None]:
    """An isolated, fully migrated database per backend."""
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


def _repositories(manager: DatabaseManager):
    if manager.db_type == "postgres":
        from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository
        from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository

        return PostgresRecommendationRepository(manager), PostgresGitOpsRepository(manager)
    from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
    from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

    return SQLiteRecommendationRepository(manager), SQLiteGitOpsRepository(manager)


def _cpu_record(*, current: int, recommended: int, created_at: datetime) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="oversized-web",
        namespace="greenkube-e2e-apps",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU,
        description=f"Reduce CPU from {current}m to {recommended}m",
        scope="workload",
        owner_kind="Deployment",
        owner_name="oversized-web",
        potential_savings_cost=86.0,
        potential_savings_co2e_grams=120.0,
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=recommended,
        created_at=created_at,
    )


async def _only_record(repo) -> RecommendationRecord:
    records = await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    assert len(records) == 1, records
    return records[0]


async def test_open_pr_freezes_the_recommendation_and_applied_values_survive_rescans(database):
    repo, gitops = _repositories(database)
    lifecycle = RecommendationLifecycle(repo)
    now = datetime.now(timezone.utc)
    proposal = _cpu_record(current=400, recommended=10, created_at=now - timedelta(minutes=30))
    assert await repo.upsert_recommendations([proposal]) == 1
    rec_id = (await _only_record(repo)).id
    assert rec_id is not None

    # Plan prepared, then the worker opens the pull request.
    assert await gitops.set_recommendation_action_status([rec_id], ActionStatus.PLAN_READY) == 1
    assert (await _only_record(repo)).status is RecommendationStatus.ACTIVE
    assert await gitops.set_recommendation_action_status([rec_id], ActionStatus.PR_OPEN, actor="automation-worker")
    opened = await _only_record(repo)
    assert opened.status is RecommendationStatus.PR_OPEN
    assert [event.event_type for event in await repo.get_events(rec_id)][-1] == "pr_opened"

    # A scan while the PR is open neither rewrites the proposal nor duplicates it.
    rescanned = _cpu_record(current=10, recommended=12, created_at=now)
    await repo.upsert_recommendations([rescanned])
    frozen = await _only_record(repo)
    assert (frozen.current_cpu_request_millicores, frozen.recommended_cpu_request_millicores) == (400, 10)
    assert frozen.status is RecommendationStatus.PR_OPEN
    # The active listing hides it from automation but the API listing shows it with its PR.
    assert await repo.get_active_recommendations() == []
    assert [r.id for r in await repo.get_active_recommendations(include_pr_open=True)] == [rec_id]

    # Merged, synced: still pr_open until the change is observed in the cluster.
    for status in (ActionStatus.MERGED, ActionStatus.SYNC_PENDING):
        assert await gitops.set_recommendation_action_status([rec_id], status) == 1
        assert (await _only_record(repo)).status is RecommendationStatus.PR_OPEN

    applied = await lifecycle.apply(rec_id, actual_cpu=10, application_method="pr_merge", actor="detector")
    assert applied.status is RecommendationStatus.APPLIED
    assert await gitops.set_recommendation_action_status([rec_id], ActionStatus.APPLIED) == 1
    assert (await _only_record(repo)).status is RecommendationStatus.APPLIED

    # The hourly rollup still carries 400m: the regenerated recommendation says 400m -> 12m.
    stale = _cpu_record(current=400, recommended=12, created_at=now)
    await repo.upsert_recommendations([stale])
    kept = await _only_record(repo)
    assert kept.status is RecommendationStatus.APPLIED
    assert (
        kept.current_cpu_request_millicores,
        kept.recommended_cpu_request_millicores,
        kept.actual_cpu_request_millicores,
    ) == (400, 10, 10)
    assert kept.cost_saved == pytest.approx(applied.cost_saved)
    assert kept.cost_saved and kept.cost_saved > 0


async def test_pull_request_closed_unmerged_returns_the_recommendation_to_active(database):
    repo, gitops = _repositories(database)
    now = datetime.now(timezone.utc)
    await repo.upsert_recommendations([_cpu_record(current=400, recommended=10, created_at=now)])
    rec_id = (await _only_record(repo)).id
    assert rec_id is not None

    await gitops.set_recommendation_action_status([rec_id], ActionStatus.PLAN_READY)
    await gitops.set_recommendation_action_status([rec_id], ActionStatus.PR_OPEN)
    assert (await _only_record(repo)).status is RecommendationStatus.PR_OPEN

    await gitops.set_recommendation_action_status([rec_id], ActionStatus.CLOSED, actor="git-provider")
    reopened = await _only_record(repo)
    assert reopened.status is RecommendationStatus.ACTIVE
    events = await repo.get_events(rec_id)
    assert [event.event_type for event in events][-1] == "pr_closed"
    assert events[-1].actor == "git-provider"

    # Active again: the next scan refreshes it in place and it can be planned again.
    await repo.upsert_recommendations([_cpu_record(current=400, recommended=20, created_at=now)])
    refreshed = await _only_record(repo)
    assert (refreshed.id, refreshed.recommended_cpu_request_millicores) == (rec_id, 20)


async def test_migration_aligns_recommendations_with_an_open_pull_request(database):
    """0061: rows written before the status sync existed move to pr_open."""
    repo, _ = _repositories(database)
    now = datetime.now(timezone.utc)
    await repo.upsert_recommendations([_cpu_record(current=400, recommended=10, created_at=now)])
    rec_id = (await _only_record(repo)).id
    async with database.connection_scope() as conn:
        if database.db_type == "postgres":
            await conn.execute("UPDATE recommendation_history SET action_status = 'pr_open' WHERE id = $1", rec_id)
        else:
            await conn.execute("UPDATE recommendation_history SET action_status = 'pr_open' WHERE id = ?", (rec_id,))
            await conn.commit()

    await _run_migration_0061(database)

    assert (await _only_record(repo)).status is RecommendationStatus.PR_OPEN


async def _run_migration_0061(database: DatabaseManager) -> None:
    from greenkube.core.migrations import runner

    script = Path(runner.__file__).parent / "scripts" / database.db_type / "0061_recommendation_pr_open_status.sql"
    async with database.connection_scope() as conn:
        if database.db_type == "postgres":
            await conn.execute(script.read_text())
        else:
            await conn.executescript(script.read_text())
            await conn.commit()


async def test_scan_within_the_rollup_lag_never_reoffers_a_change_being_verified(database):
    """Seen on Minikube (recs 291/292): a scan 74 s after the merge re-offered 400m -> 10m."""
    repo, _ = _repositories(database)
    lifecycle = RecommendationLifecycle(repo)
    now = datetime.now(timezone.utc)
    await repo.upsert_recommendations([_cpu_record(current=400, recommended=10, created_at=now - timedelta(hours=1))])
    rec_id = (await _only_record(repo)).id
    assert rec_id is not None
    await lifecycle.apply(
        rec_id, actual_cpu=10, application_method="pr_merge", actor="detector", applied_at=now - timedelta(minutes=2)
    )
    await lifecycle.mark_verification_started(rec_id)

    # The lagging rollup still reports 400m: same baseline, same or a nearby proposal.
    for regenerated in (
        _cpu_record(current=400, recommended=10, created_at=now),
        _cpu_record(current=400, recommended=12, created_at=now),
    ):
        await repo.upsert_recommendations([regenerated])
        kept = await _only_record(repo)
        assert (kept.id, kept.status) == (rec_id, RecommendationStatus.VERIFYING)
        assert (
            kept.current_cpu_request_millicores,
            kept.recommended_cpu_request_millicores,
            kept.actual_cpu_request_millicores,
        ) == (400, 10, 10)
    assert await repo.get_active_recommendations(include_pr_open=True) == []

    # Long after the change, the same advice means the request went back up: it is new advice.
    await repo.update_recommendation_fields(rec_id, {"applied_at": now - timedelta(days=2)})
    await repo.upsert_recommendations([_cpu_record(current=400, recommended=10, created_at=now)])
    (offered,) = await repo.get_active_recommendations()
    assert offered.id != rec_id and offered.current_cpu_request_millicores == 400


async def test_new_advice_after_a_change_is_still_offered(database):
    """Fresh data after the change (current = the live 10m) is a new recommendation, not a restatement."""
    repo, _ = _repositories(database)
    lifecycle = RecommendationLifecycle(repo)
    now = datetime.now(timezone.utc)
    await repo.upsert_recommendations([_cpu_record(current=400, recommended=10, created_at=now - timedelta(hours=1))])
    rec_id = (await _only_record(repo)).id
    assert rec_id is not None
    await lifecycle.apply(rec_id, actual_cpu=10, application_method="pr_merge", actor="detector")
    await lifecycle.mark_verification_started(rec_id)

    await repo.upsert_recommendations([_cpu_record(current=10, recommended=25, created_at=now)])
    (offered,) = await repo.get_active_recommendations()
    assert offered.id != rec_id
    assert (offered.current_cpu_request_millicores, offered.recommended_cpu_request_millicores) == (10, 25)


def _recorded_change(
    name: str,
    *,
    resource: str,
    status: RecommendationStatus,
    method: str,
    current: int,
    recommended: int,
    actual: int,
    cost_saved: float,
) -> RecommendationRecord:
    record = RecommendationRecord(
        pod_name=name,
        namespace="greenkube-e2e-apps",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU if resource == "cpu" else RecommendationType.RIGHTSIZING_MEMORY,
        description="Rightsizing",
        scope="workload",
        owner_kind="Deployment",
        owner_name=name,
        status=status,
        application_method=method,
        applied_at=datetime.now(timezone.utc) - timedelta(days=3),
        potential_savings_cost=86.0,
        carbon_saved_co2e_grams=cost_saved,
        cost_saved=cost_saved,
    )
    unit = "cpu_request_millicores" if resource == "cpu" else "memory_request_bytes"
    return record.model_copy(
        update={f"current_{unit}": current, f"recommended_{unit}": recommended, f"actual_{unit}": actual}
    )


async def test_migration_repairs_actual_values_reverted_by_old_rescans(database):
    """0061: merged changes whose actual_* a rescan reverted to the baseline lose that false value."""
    repo, _ = _repositories(database)
    mib = 1024 * 1024
    await repo.save_recommendations(
        [
            # Corrupted by the old refresh: actual == baseline, savings zeroed.
            _recorded_change(
                "web",
                resource="cpu",
                status=RecommendationStatus.VERIFIED,
                method="pr_merge",
                current=400,
                recommended=10,
                actual=400,
                cost_saved=0.0,
            ),
            _recorded_change(
                "web-mem",
                resource="memory",
                status=RecommendationStatus.VERIFYING,
                method="pr_merge",
                current=384 * mib,
                recommended=16 * mib,
                actual=384 * mib,
                cost_saved=0.0,
            ),
            # Correct value after the change: untouched.
            _recorded_change(
                "worker",
                resource="cpu",
                status=RecommendationStatus.VERIFIED,
                method="pr_merge",
                current=300,
                recommended=10,
                actual=10,
                cost_saved=50.0,
            ),
            # Detected outside a pull request with actual == current: genuinely unchanged, untouched.
            _recorded_change(
                "kube-rbac-proxy",
                resource="memory",
                status=RecommendationStatus.VERIFYING,
                method="detected",
                current=20 * mib,
                recommended=23 * mib,
                actual=20 * mib,
                cost_saved=0.0,
            ),
        ]
    )

    await _run_migration_0061(database)

    records = {
        record.pod_name: record
        for record in await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    }
    web = records["web"]
    assert (web.current_cpu_request_millicores, web.recommended_cpu_request_millicores) == (400, 10)
    assert web.actual_cpu_request_millicores is None
    assert web.cost_saved is None and web.carbon_saved_co2e_grams is None
    assert web.status is RecommendationStatus.VERIFIED
    assert records["web-mem"].actual_memory_request_bytes is None
    assert records["web-mem"].current_memory_request_bytes == 384 * mib
    assert records["worker"].actual_cpu_request_millicores == 10 and records["worker"].cost_saved == 50.0
    assert records["kube-rbac-proxy"].actual_memory_request_bytes == 20 * mib
    assert records["kube-rbac-proxy"].cost_saved == 0.0
