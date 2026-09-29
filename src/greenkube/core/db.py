import asyncio
import logging
from contextlib import asynccontextmanager

import aiosqlite
import asyncpg

from .config import Config, get_config
from .migrations import MigrationRunner

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manage the application's SQLite connection or PostgreSQL pool."""

    def __init__(self, config: Config | None = None):
        self._config = config
        self.connection: aiosqlite.Connection | None = None
        self.pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

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
        yield self.connection

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
        pool, connection = self.pool, self.connection
        self.pool = None
        self.connection = None
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
        await MigrationRunner("sqlite").run(self.connection)

    async def setup_postgres(self) -> None:
        if self.pool is None:
            await self.connect()
            return
        async with self.connection_scope() as connection:
            schema = self.config.DB_SCHEMA
            if not schema.replace("_", "").isalnum():
                raise ValueError("DB_SCHEMA contains invalid identifier characters")
            await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            await MigrationRunner("postgres").run(connection)


db_manager = DatabaseManager()


def get_db_manager() -> DatabaseManager:
    return db_manager
