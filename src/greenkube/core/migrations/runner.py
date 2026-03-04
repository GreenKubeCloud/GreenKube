# src/greenkube/core/migrations/runner.py
"""
Versioned migration runner for GreenKube.

Manages numbered SQL migration scripts that are applied in order.
Tracks which migrations have been applied in a ``schema_migrations`` table
so that each migration runs exactly once.

Supports both SQLite (via aiosqlite) and PostgreSQL (via asyncpg).
"""

import logging
from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)

# Location of the bundled SQL scripts (sibling ``scripts/`` package).
_SCRIPTS_PACKAGE = "greenkube.core.migrations.scripts"


class MigrationRunner:
    """Apply numbered SQL migrations to SQLite or PostgreSQL databases.

    Migration files live under ``scripts/sqlite/`` or ``scripts/postgres/``
    and follow the naming convention ``NNNN_short_description.sql`` where
    *NNNN* is a zero-padded version number (e.g. ``0001``).

    The runner:
    1. Creates a ``schema_migrations`` table if it doesn't exist.
    2. Reads which versions have already been applied.
    3. Discovers script files on disk (or inside a package).
    4. Applies any unapplied migrations **in order**.
    """

    def __init__(self, db_type: str):
        if db_type not in ("sqlite", "postgres"):
            raise ValueError(f"Unsupported db_type for migrations: {db_type}")
        self.db_type = db_type

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, connection) -> int:
        """Apply pending migrations and return the number applied.

        Args:
            connection: An ``aiosqlite.Connection`` for SQLite or an
                ``asyncpg`` connection for PostgreSQL.

        Returns:
            The count of newly applied migrations.
        """
        await self._ensure_migrations_table(connection)
        applied = await self._get_applied_versions(connection)
        scripts = self._discover_scripts()

        count = 0
        for version, name, sql in scripts:
            if version in applied:
                continue
            logger.info("Applying migration %04d: %s", version, name)
            await self._execute_sql(connection, sql)
            await self._record_migration(connection, version, name)
            count += 1

        if count:
            logger.info("Applied %d migration(s).", count)
        else:
            logger.debug("No pending migrations.")
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _discover_scripts(self) -> list[tuple[int, str, str]]:
        """Return ``[(version, name, sql), ...]`` sorted by version.

        Scripts are read from the ``scripts/<db_type>/`` sub-package so
        they are correctly included in sdist / wheel distributions.
        """
        sub_package = f"{_SCRIPTS_PACKAGE}.{self.db_type}"
        result: list[tuple[int, str, str]] = []

        try:
            script_files = resources.files(sub_package)
        except (ModuleNotFoundError, TypeError):
            # Fall back to filesystem path for editable installs
            script_dir = Path(__file__).parent / "scripts" / self.db_type
            if not script_dir.is_dir():
                logger.debug("No migration scripts directory: %s", script_dir)
                return []
            for path in sorted(script_dir.glob("*.sql")):
                version, name = self._parse_filename(path.name)
                result.append((version, name, path.read_text(encoding="utf-8")))
            return result

        for item in sorted(script_files.iterdir(), key=lambda p: p.name):
            if str(item.name).endswith(".sql"):
                version, name = self._parse_filename(item.name)
                sql = item.read_text(encoding="utf-8")
                result.append((version, name, sql))
        return result

    @staticmethod
    def _parse_filename(filename: str) -> tuple[int, str]:
        """Extract ``(version_int, description)`` from e.g. ``0001_initial.sql``."""
        stem = filename.removesuffix(".sql")
        parts = stem.split("_", 1)
        version = int(parts[0])
        name = parts[1] if len(parts) > 1 else stem
        return version, name

    # ------------------------------------------------------------------
    # SQLite-specific helpers
    # ------------------------------------------------------------------

    async def _ensure_migrations_table(self, connection) -> None:
        if self.db_type == "sqlite":
            await self._ensure_migrations_table_sqlite(connection)
        else:
            await self._ensure_migrations_table_postgres(connection)

    async def _ensure_migrations_table_sqlite(self, connection) -> None:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        await connection.commit()

    async def _ensure_migrations_table_postgres(self, connection) -> None:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            );
        """)

    async def _get_applied_versions(self, connection) -> set[int]:
        if self.db_type == "sqlite":
            return await self._get_applied_versions_sqlite(connection)
        return await self._get_applied_versions_postgres(connection)

    async def _get_applied_versions_sqlite(self, connection) -> set[int]:
        cursor = await connection.execute("SELECT version FROM schema_migrations")
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def _get_applied_versions_postgres(self, connection) -> set[int]:
        rows = await connection.fetch("SELECT version FROM schema_migrations")
        return {row["version"] for row in rows}

    async def _execute_sql(self, connection, sql: str) -> None:
        """Execute a migration script.

        Each statement in the script is separated by ``;\n`` and
        executed individually so that drivers that don't support
        multi-statement execution still work.
        """
        if self.db_type == "sqlite":
            await self._execute_sql_sqlite(connection, sql)
        else:
            await self._execute_sql_postgres(connection, sql)

    async def _execute_sql_sqlite(self, connection, sql: str) -> None:
        """Execute SQLite migration SQL statement-by-statement.

        SQLite does not support ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``.
        To make migrations safe on databases where the ``CREATE TABLE``
        already includes the columns, each statement is executed individually
        and ``OperationalError`` from duplicate-column ``ALTER TABLE`` is
        silently ignored.
        """
        import sqlite3

        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            try:
                await connection.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" in str(exc).lower():
                    logger.debug("Skipping already-applied statement: %s", exc)
                else:
                    raise
        await connection.commit()

    async def _execute_sql_postgres(self, connection, sql: str) -> None:
        await connection.execute(sql)

    async def _record_migration(self, connection, version: int, name: str) -> None:
        if self.db_type == "sqlite":
            await connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
            await connection.commit()
        else:
            await connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                version,
                name,
            )
