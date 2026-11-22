"""PostgreSQL migrations must not inherit the pool's statement timeout (upgrade safety).

The pool sets ``statement_timeout = DB_STATEMENT_TIMEOUT_MS`` on every session and the
controller migrates on a pooled connection, in one transaction. A backfill of a large
metrics table (0039) that ran longer than the timeout was cancelled, the batch rolled
back, and the controller crash-looped on every upgrade attempt.
"""

import asyncio
import os
import time
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core import config as config_module
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.migrations import MigrationRunner

pytestmark = pytest.mark.asyncio


def _dsn() -> str:
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL migration timeout tests.")
    return dsn


async def _drop_schema(dsn: str, schema: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await connection.close()


async def test_migration_longer_than_the_pool_statement_timeout_succeeds(monkeypatch):
    dsn = _dsn()
    schema = f"gk_migration_timeout_{uuid4().hex}"
    monkeypatch.setenv("DB_TYPE", "postgres")
    monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
    monkeypatch.setenv("DB_SCHEMA", schema)
    monkeypatch.setenv("DB_SSL_MODE", "disable")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "1")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "200")
    monkeypatch.setenv("DB_RUN_MIGRATIONS", "true")
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    config_module.config.reload()

    slow_backfill = [(1, "slow_backfill", "CREATE TABLE slow_marker (id INTEGER);\nSELECT pg_sleep(0.8);")]
    manager = DatabaseManager(Config())
    try:
        with patch.object(MigrationRunner, "_discover_scripts", return_value=slow_backfill):
            await manager.connect()
        assert manager.pool is not None
        async with manager.pool.acquire() as connection:
            applied = await connection.fetch("SELECT version FROM schema_migrations")
            assert [row["version"] for row in applied] == [1]
            # SET LOCAL ends with the migration transaction: the pooled session keeps
            # its configured timeout for every later query.
            assert await connection.fetchval("SHOW statement_timeout") == "200ms"
            assert await connection.fetchval("SHOW lock_timeout") == "0"
            with pytest.raises(asyncpg.exceptions.QueryCanceledError):
                await connection.execute("SELECT pg_sleep(0.8)")
    finally:
        await manager.close()
        await _drop_schema(dsn, schema)
        config_module.config.reload()


async def test_migration_blocked_by_a_table_lock_fails_within_the_lock_timeout():
    dsn = _dsn()
    schema = f"gk_migration_lock_{uuid4().hex}"
    holder = await asyncpg.connect(dsn)
    migrator = await asyncpg.connect(dsn)
    try:
        await holder.execute(f'CREATE SCHEMA "{schema}"')
        await holder.execute(f'CREATE TABLE "{schema}".busy (id INTEGER)')
        await migrator.execute(f'SET search_path TO "{schema}"')
        lock_holder = holder.transaction()
        await lock_holder.start()
        # A reader in an open transaction keeps an ACCESS SHARE lock on the table.
        await holder.execute(f'SELECT * FROM "{schema}".busy')
        runner = MigrationRunner("postgres", lock_timeout_ms=300)
        scripts = [(1, "alter_busy", "ALTER TABLE busy ADD COLUMN extra TEXT;")]
        started = time.monotonic()
        with patch.object(MigrationRunner, "_discover_scripts", return_value=scripts):
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await runner.run(migrator)
        assert time.monotonic() - started < 5
        await lock_holder.rollback()
        # The batch rolled back as a whole, so the next start applies it.
        assert await migrator.fetchval("SELECT to_regclass('schema_migrations') IS NULL")
    finally:
        await migrator.close()
        await holder.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await holder.close()


async def test_migration_waits_for_another_replica_beyond_the_lock_timeout():
    dsn = _dsn()
    schema = f"gk_migration_advisory_{uuid4().hex}"
    other_replica = await asyncpg.connect(dsn)
    migrator = await asyncpg.connect(dsn)
    runner = MigrationRunner("postgres", lock_timeout_ms=100)
    try:
        await other_replica.execute(f'CREATE SCHEMA "{schema}"')
        await migrator.execute(f'SET search_path TO "{schema}"')
        batch = other_replica.transaction()
        await batch.start()
        await other_replica.execute("SELECT pg_advisory_xact_lock($1)", runner._lock_key())
        scripts = [(1, "create_marker", "CREATE TABLE marker (id INTEGER);")]
        with patch.object(MigrationRunner, "_discover_scripts", return_value=scripts):
            pending = asyncio.create_task(runner.run(migrator))
            await asyncio.sleep(0.5)
            assert not pending.done()
            await batch.commit()
            assert await asyncio.wait_for(pending, timeout=10) == 1
    finally:
        await migrator.close()
        await other_replica.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await other_replica.close()
