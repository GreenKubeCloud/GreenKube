# tests/integration/test_configuration_overrides_migration.py
"""Migration 0059 drops the unused ``configuration_overrides`` table (roadmap M4)."""

import pytest

from greenkube.core.migrations import MigrationRunner

from .test_real_database_repositories import (  # pyrefly: ignore[missing-import]  # noqa: F401
    RealDatabase,
    _migration_versions,
    _table_names,
    real_database,
)

pytestmark = pytest.mark.database


async def test_fresh_install_has_no_configuration_overrides_table(real_database: RealDatabase):  # noqa: F811
    assert 59 in await _migration_versions(real_database)
    assert "configuration_overrides" not in await _table_names(real_database)


async def test_upgrade_drops_a_populated_configuration_overrides_table(real_database: RealDatabase):  # noqa: F811
    # Recreate the 0.3.x state: table from migration 0020 with a row, 0059 not applied yet.
    async with real_database.manager.connection_scope() as conn:
        await conn.execute(
            "CREATE TABLE configuration_overrides (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, values_json TEXT)"
        )
        await conn.execute("INSERT INTO configuration_overrides (id, version, values_json) VALUES (1, 3, '{}')")
        await conn.execute("DELETE FROM schema_migrations WHERE version = 59")
        if real_database.backend == "sqlite":
            await conn.commit()  # type: ignore[union-attr]
    assert "configuration_overrides" in await _table_names(real_database)

    async with real_database.manager.connection_scope() as conn:
        applied = await MigrationRunner(real_database.backend).run(conn)

    assert applied == 1
    assert 59 in await _migration_versions(real_database)
    assert "configuration_overrides" not in await _table_names(real_database)
