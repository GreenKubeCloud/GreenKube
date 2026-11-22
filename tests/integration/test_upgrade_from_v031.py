"""Upgrade a database seeded with the 0.3.1 schema to the current one (roadmap F17).

The kind upgrade job (hack/upgrade/upgrade-test.sh) seeds a real 0.3.1 release
with hack/upgrade/seed-v0.3.1.sql and compares hack/upgrade/counts.sql before and
after `helm upgrade`. This test runs the same seed and counts against a schema
migrated to 0037 (the last 0.3.1 migration), then applies every current
migration through the production runner with a short pool statement timeout.

It also proves the documented rollback (docs/operations/backup-restore.md):
restoring the pre-upgrade dump brings the schema back to 0.3.1, and it upgrades
again afterwards.
"""

import asyncio
import os
import re
import shlex
import shutil
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


async def _create_seeded_v031_schema(connection, schema: str, runner: MigrationRunner) -> None:
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}"')
    await runner._ensure_migrations_table(connection)
    for version, name, sql in runner._discover_scripts():
        if version > LAST_V031_MIGRATION:
            break
        await runner._execute_sql(connection, sql)
        await runner._record_migration(connection, version, name)
    await connection.execute((HACK / "seed-v0.3.1.sql").read_text(encoding="utf-8"))


async def test_seeded_v031_database_upgrades_without_losing_rows():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the 0.3.1 upgrade test.")
    schema = f"gk_upgrade_v031_{uuid4().hex}"
    connection = await asyncpg.connect(dsn, server_settings={"statement_timeout": "30000"})
    try:
        runner = MigrationRunner("postgres")
        await _create_seeded_v031_schema(connection, schema, runner)
        await connection.execute((HACK / "seed-v0.3.1.sql").read_text(encoding="utf-8"))  # idempotent
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


# -- Rollback after an upgrade = restore the pre-upgrade dump --------------------------

#: The restore pipeline of docs/operations/backup-restore.md ($1 schema, $2 connection
#: string, the archive on stdin).
RESTORE_PIPELINE = (
    '{ printf \'DROP SCHEMA IF EXISTS "%s" CASCADE;\\n\' "$1"; pg_restore --no-owner --file=-; } | '
    'psql "$2" --single-transaction -v ON_ERROR_STOP=1 -q -o /dev/null'
)


def _pg_client() -> list[str]:
    """Command prefix that runs the PostgreSQL client tools.

    ``GREENKUBE_TEST_PG_CLIENT`` may name a wrapper (CI: ``docker run --rm -i
    --network host postgres:18...``) so the client matches the server version;
    otherwise ``pg_dump`` and ``pg_restore`` from the PATH are used.
    """
    wrapper = os.getenv("GREENKUBE_TEST_PG_CLIENT", "").strip()
    if wrapper:
        return shlex.split(wrapper)
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        pytest.skip("pg_dump/pg_restore not found: set GREENKUBE_TEST_PG_CLIENT to run the restore test.")
    return []


async def _run_client(client: list[str], *args: str, stdin: bytes | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *client,
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate(stdin)
    assert process.returncode == 0, f"{args[0]} failed: {err.decode(errors='replace')}"
    return out


async def test_rollback_restores_the_pre_upgrade_dump_into_an_empty_schema():
    """The documented restore brings a migrated schema back to 0.3.1, and it upgrades again.

    ``pg_restore --clean`` cannot do this: objects created by newer migrations (foreign
    keys to recommendation_history, views on the ledger) are not in the pre-upgrade dump
    and block the drops. The procedure drops the schema first and restores into it.
    """
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the restore test.")
    client = _pg_client()
    client_dsn = os.getenv("GREENKUBE_TEST_PG_CLIENT_DSN", dsn)
    version = (await _run_client(client, "pg_dump", "--version")).decode()
    connection = await asyncpg.connect(dsn, server_settings={"statement_timeout": "30000"})
    schema = f"gk_restore_v031_{uuid4().hex}"
    try:
        server_major = int(str(await connection.fetchval("SHOW server_version_num"))) // 10000
        match = re.search(r"(\d+)\.", version)
        if match is None or int(match.group(1)) < server_major:
            pytest.skip(f"pg_dump ({version.strip()}) is older than the server ({server_major}).")
        runner = MigrationRunner("postgres")
        await _create_seeded_v031_schema(connection, schema, runner)
        before = await _counts(connection)

        # Same dump as the backup CronJob: the GreenKube schema, custom format.
        dump = await _run_client(
            client, "pg_dump", f"--dbname={client_dsn}", f"--schema={schema}", "--format=custom", "--no-owner"
        )
        assert await runner.run(connection) > 0
        assert await connection.fetchval("SELECT max(version) FROM schema_migrations") == runner.latest_version()

        # Documented restore (backup-restore.md): DROP SCHEMA and the replayed dump in one transaction.
        await _run_client(client, "sh", "-c", RESTORE_PIPELINE, "sh", schema, client_dsn, stdin=dump)

        await connection.execute(f'SET search_path TO "{schema}"')
        assert await connection.fetchval("SELECT max(version) FROM schema_migrations") == LAST_V031_MIGRATION
        assert await _counts(connection) == before
        leftover = await connection.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = $1 AND tablename = 'recommendation_revisions'", schema
        )
        assert leftover == 0, "a table from a newer migration survived the restore"

        # Upgrading again after the rollback works: nothing is left from the first try.
        assert await runner.run(connection) > 0
        assert await connection.fetchval("SELECT max(version) FROM schema_migrations") == runner.latest_version()
        assert await _counts(connection) == before
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
