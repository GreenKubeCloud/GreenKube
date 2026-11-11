"""First-seen persistence of deletion candidates and the 0063 retirement, on SQLite and PostgreSQL.

PostgreSQL cases run when ``GREENKUBE_TEST_POSTGRES_DSN`` is set. Timestamps
are derived from the current time.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.migrations import MigrationRunner
from greenkube.storage.deletion_candidate_repository import (
    CandidateObservation,
    DeletionCandidateRepository,
    deletion_candidate_repository_for,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
RETIREMENT_VERSION = 63
LB = "LoadBalancerService"
PV = "PersistentVolume"


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def repo(request, tmp_path, monkeypatch) -> AsyncGenerator[DeletionCandidateRepository, None]:
    name = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if name == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL deletion-candidate tests.")
        schema = f"gk_deletion_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "candidates.sqlite"))
        monkeypatch.setenv("DB_SCHEMA", "public")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield DeletionCandidateRepository(manager)
    finally:
        await manager.close()
        if name == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _obs(name, namespace="legacy", uid="uid-1", evidence=("selector matches no pod",)):
    return CandidateObservation(namespace=namespace, name=name, uid=uid, evidence=list(evidence))


class TestObserve:
    async def test_first_observation_starts_the_clock(self, repo):
        first = await repo.observe(LB, [_obs("old-api")], now=NOW)
        assert first == {("legacy", "old-api"): NOW}
        rows = await repo.list(LB)
        assert [(r["namespace"], r["name"], r["uid"], r["evidence"]) for r in rows] == [
            ("legacy", "old-api", "uid-1", ["selector matches no pod"])
        ]

    async def test_continuous_observations_keep_first_seen(self, repo):
        first: dict = {}
        for hours in range(0, 24 * 8 + 1, 24):
            first = await repo.observe(LB, [_obs("old-api")], now=NOW + timedelta(hours=hours))
        assert first[("legacy", "old-api")] == NOW
        assert (await repo.list(LB))[0]["last_seen_at"] == NOW + timedelta(days=8)

    async def test_a_gap_restarts_the_clock(self, repo):
        await repo.observe(LB, [_obs("old-api")], now=NOW)
        later = NOW + timedelta(days=3)
        first = await repo.observe(LB, [_obs("old-api")], now=later)
        assert first[("legacy", "old-api")] == later

    async def test_a_recreated_object_restarts_the_clock(self, repo):
        await repo.observe(LB, [_obs("old-api")], now=NOW)
        later = NOW + timedelta(hours=1)
        first = await repo.observe(LB, [_obs("old-api", uid="uid-2")], now=later)
        assert first[("legacy", "old-api")] == later

    async def test_resources_back_in_use_are_forgotten(self, repo):
        await repo.observe(LB, [_obs("old-api"), _obs("other")], now=NOW)
        await repo.observe(LB, [_obs("other")], now=NOW + timedelta(hours=1))
        assert [r["name"] for r in await repo.list(LB)] == ["other"]
        later = NOW + timedelta(hours=2)
        first = await repo.observe(LB, [_obs("old-api"), _obs("other")], now=later)
        assert first[("legacy", "old-api")] == later
        assert first[("legacy", "other")] == NOW

    async def test_kinds_are_independent(self, repo):
        await repo.observe(LB, [_obs("old-api")], now=NOW)
        await repo.observe(PV, [_obs("pvc-1", namespace="")], now=NOW)
        await repo.observe(PV, [], now=NOW + timedelta(hours=1))
        assert [r["name"] for r in await repo.list()] == ["old-api"]

    async def test_repository_for_sql_backends(self, repo):
        from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
        from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

        cls = PostgresCombinedMetricsRepository if repo._postgres else SQLiteCombinedMetricsRepository
        assert isinstance(deletion_candidate_repository_for(cls(repo.db_manager)), DeletionCandidateRepository)
        assert deletion_candidate_repository_for(object()) is None


# --------------------------------------------------------------------------- #
# Migration 0063: legacy deletion recommendations are retired with an event   #
# --------------------------------------------------------------------------- #

LEGACY_ROWS = [
    ("cilium-gateway-cluster-gateway", "gateway", "ORPHANED_LOAD_BALANCER", "active"),
    ("pvc-1", None, "ORPHANED_PERSISTENT_VOLUME", "active"),
    ("ignored-lb", "legacy", "ORPHANED_LOAD_BALANCER", "ignored"),
    ("web", "shop", "RIGHTSIZING_CPU", "active"),
]
EXPECTED_STATUSES = {
    "cilium-gateway-cluster-gateway": "stale",
    "pvc-1": "stale",
    "ignored-lb": "ignored",
    "web": "active",
}


def _check_events(events):
    assert sorted(name for name, _type, _payload in events) == ["cilium-gateway-cluster-gateway", "pvc-1"]
    for _name, event_type, payload in events:
        assert event_type == "retired"
        details = json.loads(payload)["details"]
        assert details["code"] == "deletion_evidence_required"
        assert details["to_status"] == "stale"


async def test_sqlite_upgrade_retires_legacy_deletion_recommendations(tmp_path):
    db_file = str(tmp_path / "legacy.db")
    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(db_file) as connection:
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version >= RETIREMENT_VERSION:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.commit()
    created = (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            "INSERT INTO recommendation_history (pod_name, namespace, type, description, status, created_at) "
            "VALUES (?, ?, ?, 'legacy', ?, ?)",
            [(*row, created) for row in LEGACY_ROWS],
        )

    async with aiosqlite.connect(db_file) as connection:
        assert await MigrationRunner("sqlite").run(connection) >= 1
        assert await MigrationRunner("sqlite").run(connection) == 0

    with sqlite3.connect(db_file) as connection:
        statuses = dict(connection.execute("SELECT pod_name, status FROM recommendation_history"))
        events = list(
            connection.execute(
                "SELECT h.pod_name, e.event_type, e.payload FROM recommendation_events e "
                "JOIN recommendation_history h ON h.id = e.recommendation_id"
            )
        )
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert statuses == EXPECTED_STATUSES
    _check_events(events)
    assert "deletion_candidates" in tables


async def test_postgres_upgrade_retires_legacy_deletion_recommendations():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL migration upgrade tests.")
    schema = f"gk_retire_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        runner = MigrationRunner("postgres")
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version >= RETIREMENT_VERSION:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.executemany(
            "INSERT INTO recommendation_history (pod_name, namespace, type, description, status, created_at) "
            "VALUES ($1, $2, $3, 'legacy', $4, $5)",
            [(*row, NOW - timedelta(days=3)) for row in LEGACY_ROWS],
        )

        assert await MigrationRunner("postgres").run(connection) >= 1
        assert await MigrationRunner("postgres").run(connection) == 0

        statuses = {
            row["pod_name"]: row["status"]
            for row in await connection.fetch("SELECT pod_name, status FROM recommendation_history")
        }
        events = [
            (row["pod_name"], row["event_type"], row["payload"])
            for row in await connection.fetch(
                "SELECT h.pod_name, e.event_type, e.payload FROM recommendation_events e "
                "JOIN recommendation_history h ON h.id = e.recommendation_id"
            )
        ]
        assert statuses == EXPECTED_STATUSES
        _check_events(events)
        assert await connection.fetchval("SELECT to_regclass('deletion_candidates')") is not None
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
