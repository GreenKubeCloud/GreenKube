"""Upgrade a database seeded with the 0.3.1 schema to the current one (roadmap F17).

The kind upgrade job (hack/upgrade/upgrade-test.sh) seeds a real 0.3.1 release
with hack/upgrade/seed-v0.3.1.sql and compares hack/upgrade/counts.sql before and
after `helm upgrade`. This test runs the same seed and counts against a schema
migrated to 0037 (the last 0.3.1 migration), then applies every current
migration through the production runner with a short pool statement timeout.
"""

import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner

pytestmark = pytest.mark.asyncio

HACK = Path(__file__).resolve().parents[2] / "hack" / "upgrade"
LAST_V031_MIGRATION = 37


async def _counts(connection) -> dict[str, int]:
    rows = await connection.fetch((HACK / "counts.sql").read_text(encoding="utf-8"))
    return {row["name"]: row["rows"] for row in rows}


async def test_seeded_v031_database_upgrades_without_losing_rows():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the 0.3.1 upgrade test.")
    schema = f"gk_upgrade_v031_{uuid4().hex}"
    connection = await asyncpg.connect(dsn, server_settings={"statement_timeout": "30000"})
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        runner = MigrationRunner("postgres")
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version > LAST_V031_MIGRATION:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)

        seed = (HACK / "seed-v0.3.1.sql").read_text(encoding="utf-8")
        await connection.execute(seed)
        await connection.execute(seed)  # idempotent
        before = await _counts(connection)
        assert before == {
            "combined_metrics": 2400,
            "container_metrics": 4800,
            "recommendation_history": 20,
            "recommendation_events": 40,
            "recommendation_savings_ledger": 60,
        }

        applied = await runner.run(connection)

        assert applied > 0
        assert await runner.pending_versions(connection) == []
        assert await connection.fetchval("SELECT max(version) FROM schema_migrations") == runner.latest_version()
        assert await _counts(connection) == before
        # The 0039 backfill gave every legacy metric row its provenance.
        assert await connection.fetchval("SELECT count(*) FROM combined_metrics WHERE cost_source IS NULL") == 0
        assert await connection.fetchval("SELECT count(*) FROM container_metrics WHERE cost_source IS NULL") == 0
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
