import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiosqlite
import asyncpg

from .config import Config, get_config
from .migrations import MigrationRunner
from .migrations.online import complete_online_column_upgrades, pending_online_upgrades

logger = logging.getLogger(__name__)


class _ReentrantAsyncLock:
    """Serialize access to the shared SQLite connection, including nested scopes."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("SQLite connection access requires an asyncio task")
        if self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        if self._owner is not asyncio.current_task():
            raise RuntimeError("SQLite connection lock released by a non-owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "_ReentrantAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    @property
    def depth(self) -> int:
        """Nesting depth held by the current owner (0 when unowned)."""
        return self._depth if self._owner is asyncio.current_task() else 0


async def discard_leaked_sqlite_transaction(connection: aiosqlite.Connection) -> None:
    """Roll back a transaction left open by a previous scope so it cannot poison later writes.

    Callers must hold the SQLite operation lock at the outermost depth: no other scope
    is then legitimately inside a transaction.
    """
    if connection.in_transaction:
        logger.warning("Rolling back a SQLite transaction leaked by a previous database scope.")
        await connection.rollback()


async def _rollback_quietly(connection: aiosqlite.Connection) -> None:
    try:
        if connection.in_transaction:
            await connection.rollback()
    except Exception:
        logger.exception("SQLite rollback after a failed database scope failed.")


async def warn_on_exhausting_sequences(connection: Any, threshold: float = 0.5) -> list[str]:
    """Log a warning for every sequence of the schema above ``threshold`` of its maximum (STO-05).

    Returns the names of those sequences.
    """
    rows = await connection.fetch(
        "SELECT sequencename, last_value, max_value FROM pg_sequences "
        "WHERE schemaname = current_schema() AND last_value IS NOT NULL AND last_value > max_value * $1::float8",
        threshold,
    )
    for row in rows:
        logger.warning(
            "Sequence %s is at %d of %d (%.0f%%): writes fail once it reaches its maximum.",
            row["sequencename"],
            row["last_value"],
            row["max_value"],
            100.0 * row["last_value"] / row["max_value"],
        )
    return [row["sequencename"] for row in rows]


@dataclass(frozen=True)
class SchemaStatus:
    """Migration state of the connected database."""

    current: bool
    pending: tuple[int, ...]
    expected_version: int


class DatabaseManager:
    """Manage the application's SQLite connection or PostgreSQL pool."""

    def __init__(self, config: Config | None = None):
        self._config = config
        self.connection: aiosqlite.Connection | None = None
        self.pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._sqlite_operation_lock = _ReentrantAsyncLock()
        self._savepoint_counter = 0
        self._schema_current = False
        #: Background backfill of large-table column upgrades started by the migrating process (NRG-06).
        self._online_upgrade_task: asyncio.Task | None = None
        #: Seconds between two attempts of a pending online upgrade (a swap that missed its lock).
        self.online_upgrade_retry_seconds: float = 600.0

    @property
    def config(self) -> Config:
        return self._config if self._config is not None else get_config()

    @property
    def db_type(self) -> str:
        return self.config.DB_TYPE

    async def connect(self) -> None:
        async with self._lock:
            if self.connection is not None or self.pool is not None:
                return
            if self.db_type == "sqlite":
                self.connection = await aiosqlite.connect(self.config.DB_PATH)
                self.connection.row_factory = aiosqlite.Row
                try:
                    await self.setup_sqlite()
                except BaseException:
                    await self.close()
                    raise
                return
            if self.db_type == "postgres":
                ssl_arg = None if self.config.DB_SSL_MODE == "disable" else self.config.DB_SSL_MODE
                try:
                    self.pool = await asyncpg.create_pool(
                        dsn=self.config.DB_CONNECTION_STRING,
                        ssl=ssl_arg,
                        min_size=self.config.DB_POOL_MIN_SIZE,
                        max_size=self.config.DB_POOL_MAX_SIZE,
                        server_settings={
                            "search_path": self.config.DB_SCHEMA,
                            "statement_timeout": str(self.config.DB_STATEMENT_TIMEOUT_MS),
                            # Day, week, month and year buckets are UTC whatever the server default (STO-10).
                            "timezone": "UTC",
                        },
                    )
                    await self.setup_postgres()
                except BaseException:
                    await self.close()
                    raise
                return
            raise ValueError(f"Unsupported database type specified in config: {self.db_type}")

    @asynccontextmanager
    async def connection_scope(self):
        if self.db_type == "postgres":
            await self.ensure_connection()
            assert self.pool is not None
            async with self.pool.acquire() as connection:
                yield connection
            return
        await self.ensure_connection()
        assert self.connection is not None
        async with self._sqlite_operation_lock:
            connection = self.connection
            outermost = self._sqlite_operation_lock.depth == 1
            if outermost:
                await discard_leaked_sqlite_transaction(connection)
            try:
                yield connection
            except BaseException:
                # An exception between a write and its commit must never leave the shared
                # connection inside a transaction (BUG-50).
                if outermost:
                    await _rollback_quietly(connection)
                raise

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False):
        """Run one write transaction: commit on success, always roll back on error.

        SQLite: serialized on the shared connection. When the calling task is already
        inside a transaction (for example a ``UnitOfWork``), the block runs in a
        SAVEPOINT so a failure only undoes its own statements.
        PostgreSQL: always a dedicated pooled connection and transaction. It does not join a
        ``UnitOfWork`` opened by the same task, so never call it while an outer PostgreSQL
        transaction holds row locks it would need (it would wait on them); run those statements
        on the ``UnitOfWork`` connection instead.
        """
        if self.db_type == "postgres":
            await self.ensure_connection()
            assert self.pool is not None
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    yield connection
            return

        async with self.connection_scope() as connection:
            if self._sqlite_operation_lock.depth > 1 and connection.in_transaction:
                self._savepoint_counter += 1
                savepoint = f"greenkube_tx_{self._savepoint_counter}"
                await connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield connection
                except BaseException:
                    await connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    await connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
                await connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                return

            await discard_leaked_sqlite_transaction(connection)
            await connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                await _rollback_quietly(connection)
                raise
            await connection.commit()

    async def ensure_connection(self) -> None:
        if self.db_type == "postgres":
            if self.pool is None:
                await self.connect()
            return
        if self.connection is not None:
            try:
                async with self.connection.execute("SELECT 1"):
                    return
            except Exception:
                logger.warning("SQLite connection was closed or invalid; reconnecting.")
                self.connection = None
        await self.connect()

    async def close(self) -> None:
        task, self._online_upgrade_task = self._online_upgrade_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        pool, connection = self.pool, self.connection
        self.pool = None
        self.connection = None
        self._schema_current = False
        if pool is not None:
            await pool.close()
        if connection is not None:
            await connection.close()

    async def setup_sqlite(self, db_path: str | None = None) -> None:
        if db_path is not None:
            if self.connection is not None:
                await self.connection.close()
            self.connection = await aiosqlite.connect(db_path)
            self.connection.row_factory = aiosqlite.Row
        if self.connection is None:
            await self.connect()
            return
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA cache_size=-65536")
        await self.connection.execute("PRAGMA mmap_size=268435456")
        await self.connection.execute("PRAGMA synchronous=NORMAL")
        # A writer waits up to 30 s for another process's lock instead of failing at once (STO-04).
        await self.connection.execute("PRAGMA busy_timeout=30000")
        await MigrationRunner("sqlite").run(self.connection)
        self._schema_current = True

    async def setup_postgres(self) -> None:
        if self.pool is None:
            await self.connect()
            return
        async with self.connection_scope() as connection:
            schema = self.config.DB_SCHEMA
            if not schema.replace("_", "").isalnum():
                raise ValueError("DB_SCHEMA contains invalid identifier characters")
            if not self.config.DB_RUN_MIGRATIONS:
                # Only the controller migrates (F11); this process waits for it and
                # reports not-ready meanwhile (see schema_status).
                logger.info("Schema migrations are run by the controller (DB_RUN_MIGRATIONS=false).")
                return
            await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            await MigrationRunner("postgres").run(connection)
            self._schema_current = True
            await warn_on_exhausting_sequences(connection)
            if await pending_online_upgrades(connection):
                self._online_upgrade_task = asyncio.create_task(self._complete_online_upgrades())

    async def _complete_online_upgrades(self) -> None:
        """Backfill and swap the large-table column upgrades left by migrations, in the background (NRG-06).

        A swap that missed its lock (or a failed attempt) is retried every
        ``online_upgrade_retry_seconds`` until nothing is pending.
        """
        while True:
            try:
                async with self.connection_scope() as connection:
                    await complete_online_column_upgrades(connection)
                    if not await pending_online_upgrades(connection):
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Online column upgrade failed; it is retried.")
            await asyncio.sleep(self.online_upgrade_retry_seconds)

    async def schema_status(self) -> "SchemaStatus":
        """Whether every shipped migration is applied (F11 API readiness).

        Once current, the answer is cached for the life of the process: migrations
        only move forward while a release runs.
        """
        expected = MigrationRunner(self.db_type).latest_version()
        if self._schema_current:
            return SchemaStatus(current=True, pending=(), expected_version=expected)
        async with self.connection_scope() as connection:
            pending = tuple(await MigrationRunner(self.db_type).pending_versions(connection))
        current = not pending
        if current:
            self._schema_current = True
        return SchemaStatus(current=current, pending=pending, expected_version=expected)


db_manager = DatabaseManager()


def get_db_manager() -> DatabaseManager:
    return db_manager
