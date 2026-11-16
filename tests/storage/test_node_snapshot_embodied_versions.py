"""Node snapshot versions stay stable and carry only the operator embodied value (NRG-07).

Node analysis and the collection used to save the same nodes with ``embodied_emissions_kg``
None and 100 in turn, opening a new SCD version on every cycle. Migration 0181 clears the
derived values and collapses the identical consecutive versions they produced.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.embodied_service import EmbodiedEmissionsService
from greenkube.core.migrations import runner as migration_runner
from greenkube.models.node import NodeInfo
from greenkube.storage.postgres.node_repository import PostgresNodeRepository
from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository

T0 = datetime(2026, 11, 14, 21, 50, tzinfo=timezone.utc)
SCRIPTS = Path(migration_runner.__file__).resolve().parent / "scripts"


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def manager(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_nodescd_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "nodes.sqlite"))
    config.reload()
    db = DatabaseManager(Config())
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _repository(db: DatabaseManager):
    return PostgresNodeRepository(db) if db.db_type == "postgres" else SQLiteNodeRepository(db)


def _node(ts: datetime, **overrides) -> NodeInfo:
    values = {
        "name": "minikube",
        "instance_type": "cpu-4",
        "cloud_provider": "unknown",
        "cpu_capacity_cores": 4.0,
        "memory_capacity_bytes": 8 * 1024**3,
        "timestamp": ts,
    }
    values.update(overrides)
    return NodeInfo(**values)


async def _versions(db: DatabaseManager) -> list[tuple]:
    query = "SELECT valid_from, valid_to, is_current, embodied_emissions_kg FROM node_snapshots_scd ORDER BY valid_from"
    async with db.connection_scope() as scope:
        conn: Any = scope
        if db.db_type == "postgres":
            return [tuple(row) for row in await conn.fetch(query)]
        async with conn.execute(query) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


async def _execute_script(db: DatabaseManager, name: str) -> None:
    dialect = "postgres" if db.db_type == "postgres" else "sqlite"
    sql = (SCRIPTS / dialect / name).read_text()
    runner = migration_runner.MigrationRunner(dialect)
    async with db.connection_scope() as scope:
        conn: Any = scope
        if dialect == "postgres":
            async with conn.transaction():
                await runner._execute_sql(conn, sql)
        else:
            await runner._execute_sql(conn, sql)
            await conn.commit()


async def test_alternating_node_analysis_and_collection_keep_one_version(manager):
    repository = _repository(manager)
    embodied_repository = MagicMock()
    embodied_repository.get_profile = AsyncMock(return_value=None)
    embodied_repository.save_profile = AsyncMock()
    service = EmbodiedEmissionsService(
        boavizta_collector=MagicMock(),
        embodied_repository=embodied_repository,
        node_repository=repository,  # type: ignore[arg-type]
        calculator=MagicMock(),
        estimator=MagicMock(),
        config=Config(),
    )
    for cycle in range(50):
        ts = T0 + timedelta(seconds=30 * cycle)
        await repository.save_nodes([_node(ts)])  # node-analysis loop
        await service.prepare_embodied_data({"minikube": _node(ts)}, now=ts)  # every run_range
    versions = await _versions(manager)
    assert len(versions) == 1
    assert versions[0][3] is None


async def test_migration_0181_clears_derived_values_and_collapses_flip_flop_versions(manager):
    repository = _repository(manager)
    # The pre-fix writers: NULL (node analysis) and 100 (embodied service) in turn, then a real change.
    for cycle in range(6):
        ts = T0 + timedelta(seconds=30 * cycle)
        await repository.save_nodes([_node(ts, embodied_emissions_kg=None if cycle % 2 == 0 else 100.0)])
    await repository.save_nodes([_node(T0 + timedelta(minutes=10), cpu_capacity_cores=8.0)])
    assert len(await _versions(manager)) == 7

    await _execute_script(manager, "0181_node_snapshot_embodied_configured.sql")

    versions = await _versions(manager)
    assert len(versions) == 2, versions
    first, second = versions
    assert all(version[3] is None for version in versions)
    assert not first[2] and second[2], "the collapsed version is closed, the real change is current"
    assert first[1] is not None and second[1] is None
