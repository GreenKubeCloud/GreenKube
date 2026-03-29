# src/greenkube/core/db.py

import asyncio
import logging
from contextlib import asynccontextmanager

import aiosqlite
import asyncpg

from .config import Config, get_config
from .migrations import MigrationRunner

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages the asynchronous connection to the database (SQLite or PostgreSQL).

    Accepts an optional :class:`Config` instance for dependency injection.
    When *config* is ``None`` the module-level singleton is used.
    """

    def __init__(self, config: Config | None = None):
        self._config = config
        self.connection = None
        self.pool = None
        self._lock = asyncio.Lock()

    @property
    def config(self) -> Config:
        """Resolve the config lazily so the latest singleton is always used."""
        if self._config is not None:
            return self._config
        return get_config()

    @property
    def db_type(self):
        return self.config.DB_TYPE

    async def connect(self):
        """
        Establishes a connection to the configured database.
        """
        async with self._lock:
            if self.connection or self.pool:
                return

            try:
                if self.db_type == "sqlite":
                    self.connection = await aiosqlite.connect(self.config.DB_PATH)
                    self.connection.row_factory = aiosqlite.Row
                    logger.info("Successfully connected to SQLite database.")
                    await self.setup_sqlite()
                elif self.db_type == "postgres":
                    try:
                        self.pool = await asyncpg.create_pool(
                            dsn=self.config.DB_CONNECTION_STRING,
                            min_size=1,
                            max_size=10,
                            server_settings={"search_path": self.config.DB_SCHEMA},
                        )
                        logger.info("Successfully initialized PostgreSQL connection pool.")
                        await self.setup_postgres()
                    except Exception as e:
                        logger.error("Failed to initialize PostgreSQL connection pool: %s", e)
                        raise
                elif self.db_type == "elasticsearch":
                    # No-op: Connection is handled by ElasticsearchCarbonIntensityRepository
                    logger.info("DB_TYPE is 'elasticsearch'. Connection will be managed by the specific repository.")
                    self.connection = None
                else:
                    raise ValueError("Unsupported database type specified in config.")
            except Exception as e:
                logger.error("Could not connect to the database: %s", e)
                raise

    @asynccontextmanager
    async def connection_scope(self):
        """
        Yields a database connection.
        For PostgreSQL, gets a connection from the pool.
        For SQLite, yields the single persistent connection.
        """
        if self.db_type == "postgres":
            if not self.pool:
                await self.connect()
            async with self.pool.acquire() as conn:
                yield conn
        else:
            await self.ensure_connection()
            yield self.connection

    async def ensure_connection(self):
        """
        Checks if the connection is alive and reconnects if necessary.
        """
        if self.db_type == "postgres":
            if self.pool is None:
                await self.connect()
            return

        # Fast check without lock first
        if self.connection is not None:
            # Verify connection is alive
            try:
                async with self.connection.execute("SELECT 1") as cursor:
                    await cursor.fetchone()
                return
            except Exception:
                logger.warning("SQLite connection was closed or invalid. Reconnecting.")

        # Reconnect with lock
        await self.connect()

    async def close(self):
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL connection pool closed.")
        if self.connection:
            await self.connection.close()
            logger.info("Database connection closed.")

    async def setup_sqlite(self, db_path: str = None):
        """
        Creates the necessary tables for SQLite if they don't exist.
        """
        if db_path:
            self.connection = await aiosqlite.connect(db_path)
            self.connection.row_factory = aiosqlite.Row

        if not self.connection:
            logger.error("Cannot setup SQLite, no connection available.")
            return

        try:
            # Enable WAL mode for significantly better concurrent read performance.
            # WAL allows readers and the single writer to operate without blocking each other.
            await self.connection.execute("PRAGMA journal_mode=WAL;")
            # Increase the in-memory page cache (negative value = kibibytes).
            # 64 MB cache avoids repeated disk I/O for hot query paths.
            await self.connection.execute("PRAGMA cache_size=-65536;")
            # Allow OS-level read-ahead; safe for WAL mode.
            await self.connection.execute("PRAGMA mmap_size=268435456;")
            # Relax fsync frequency — acceptable for a demo/dev SQLite database.
            await self.connection.execute("PRAGMA synchronous=NORMAL;")

            # --- Table for carbon_intensity_history ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS carbon_intensity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone TEXT NOT NULL,
                    carbon_intensity REAL NOT NULL,
                    datetime TEXT NOT NULL,
                    updated_at TEXT,
                    created_at TEXT,
                    emission_factor_type TEXT,
                    is_estimated BOOLEAN,
                    estimation_method TEXT,
                    UNIQUE(zone, datetime)
                );
            """)

            # --- Table for node_power_consumption ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS node_power_consumption (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_name TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    power_consumption_mw INTEGER NOT NULL,
                    UNIQUE(node_name, timestamp)
                );
            """)

            # --- Table for pod_resource_usage ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS pod_resource_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pod_name TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    cpu_usage_milli_cores REAL,
                    memory_usage_bytes INTEGER,
                    UNIQUE(pod_name, namespace, timestamp)
                );
            """)

            # --- Table for combined_metrics ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS combined_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pod_name TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    total_cost REAL,
                    co2e_grams REAL,
                    pue REAL,
                    grid_intensity REAL,
                    joules REAL,
                    cpu_request INTEGER,
                    memory_request INTEGER,
                    cpu_usage_millicores INTEGER,
                    memory_usage_bytes INTEGER,
                    network_receive_bytes REAL,
                    network_transmit_bytes REAL,
                    disk_read_bytes REAL,
                    disk_write_bytes REAL,
                    storage_request_bytes INTEGER,
                    storage_usage_bytes INTEGER,
                    ephemeral_storage_request_bytes INTEGER,
                    ephemeral_storage_usage_bytes INTEGER,
                    gpu_usage_millicores INTEGER,
                    restart_count INTEGER,
                    owner_kind TEXT,
                    owner_name TEXT,
                    period TEXT,
                    "timestamp" TEXT,
                    duration_seconds INTEGER,
                    grid_intensity_timestamp TEXT,
                    node TEXT,
                    node_instance_type TEXT,
                    node_zone TEXT,
                    emaps_zone TEXT,
                    is_estimated BOOLEAN,
                    estimation_reasons TEXT,
                    embodied_co2e_grams REAL,
                    calculation_version TEXT,
                    UNIQUE(pod_name, namespace, "timestamp")
                );
            """)

            # --- Table for node_snapshots ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS node_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    instance_type TEXT,
                    cpu_capacity_cores REAL,
                    architecture TEXT,
                    cloud_provider TEXT,
                    region TEXT,
                    zone TEXT,
                    node_pool TEXT,
                    memory_capacity_bytes INTEGER,
                    embodied_emissions_kg REAL,
                    UNIQUE(node_name, timestamp)
                );
            """)

            # --- Table for instance_carbon_profiles ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS instance_carbon_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    instance_type TEXT NOT NULL,
                    gwp_manufacture REAL NOT NULL,
                    lifespan_hours INTEGER NOT NULL,
                    source TEXT,
                    last_updated TEXT,
                    UNIQUE(provider, instance_type)
                );
            """)

            # --- Table for recommendation_history ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS recommendation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pod_name TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    reason TEXT,
                    priority TEXT,
                    potential_savings_cost REAL,
                    potential_savings_co2e_grams REAL,
                    current_cpu_request_millicores INTEGER,
                    recommended_cpu_request_millicores INTEGER,
                    current_memory_request_bytes INTEGER,
                    recommended_memory_request_bytes INTEGER,
                    cron_schedule TEXT,
                    target_node TEXT,
                    created_at TEXT NOT NULL
                );
            """)

            # --- Performance indexes ---
            # These indexes are critical for the dashboard query performance.
            # Without them, every API request does a full table scan.
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_combined_metrics_timestamp
                ON combined_metrics ("timestamp");
            """)
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_combined_metrics_namespace_timestamp
                ON combined_metrics (namespace, "timestamp");
            """)
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_carbon_intensity_zone_datetime
                ON carbon_intensity_history (zone, datetime);
            """)
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_recommendation_history_namespace
                ON recommendation_history (namespace);
            """)

            # Run versioned migrations
            runner = MigrationRunner("sqlite")
            await runner.run(self.connection)

            await self.connection.commit()
            logger.info("SQLite schema is up to date.")
        except Exception as e:
            logger.error("Error checking/creating SQLite tables: %s", e)

    async def setup_postgres(self):
        """
        Creates the necessary tables for PostgreSQL if they don't exist.
        """
        if not self.pool:
            logger.error("Cannot setup Postgres, no connection pool available.")
            return

        async with self.connection_scope() as conn:
            # Create schema if it doesn't exist
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.config.DB_SCHEMA};")

            # --- Table for carbon_intensity_history ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS carbon_intensity_history (
                    id SERIAL PRIMARY KEY,
                    zone TEXT NOT NULL,
                    carbon_intensity DOUBLE PRECISION NOT NULL,
                    datetime TIMESTAMP WITH TIME ZONE NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP WITH TIME ZONE,
                    emission_factor_type TEXT,
                    is_estimated BOOLEAN,
                    estimation_method TEXT,
                    UNIQUE(zone, datetime)
                );
            """)

            # --- Table for node_power_consumption ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS node_power_consumption (
                    id SERIAL PRIMARY KEY,
                    node_name TEXT NOT NULL,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    power_consumption_mw INTEGER NOT NULL,
                    UNIQUE(node_name, timestamp)
                );
            """)

            # --- Table for pod_resource_usage ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pod_resource_usage (
                    id SERIAL PRIMARY KEY,
                    pod_name TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    cpu_usage_milli_cores REAL,
                    memory_usage_bytes BIGINT,
                    UNIQUE(pod_name, namespace, timestamp)
                );
            """)

            # --- Table for combined_metrics ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS combined_metrics (
                    id SERIAL PRIMARY KEY,
                    pod_name TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    total_cost REAL,
                    co2e_grams REAL,
                    pue REAL,
                    grid_intensity REAL,
                    joules REAL,
                    cpu_request INTEGER,
                    memory_request BIGINT,
                    cpu_usage_millicores INTEGER,
                    memory_usage_bytes BIGINT,
                    network_receive_bytes DOUBLE PRECISION,
                    network_transmit_bytes DOUBLE PRECISION,
                    disk_read_bytes DOUBLE PRECISION,
                    disk_write_bytes DOUBLE PRECISION,
                    storage_request_bytes BIGINT,
                    storage_usage_bytes BIGINT,
                    ephemeral_storage_request_bytes BIGINT,
                    ephemeral_storage_usage_bytes BIGINT,
                    gpu_usage_millicores INTEGER,
                    restart_count INTEGER,
                    owner_kind TEXT,
                    owner_name TEXT,
                    period TEXT,
                    timestamp TIMESTAMP WITH TIME ZONE,
                    duration_seconds INTEGER,
                    grid_intensity_timestamp TIMESTAMP WITH TIME ZONE,
                    node TEXT,
                    node_instance_type TEXT,
                    node_zone TEXT,
                    emaps_zone TEXT,
                    is_estimated BOOLEAN,
                    estimation_reasons TEXT,
                    embodied_co2e_grams REAL,
                    calculation_version TEXT,
                    UNIQUE(pod_name, namespace, timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_combined_metrics_timestamp ON combined_metrics(timestamp);
            """)

            # --- Table for node_snapshots ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS node_snapshots (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    node_name TEXT NOT NULL,
                    instance_type TEXT,
                    cpu_capacity_cores REAL,
                    architecture TEXT,
                    cloud_provider TEXT,
                    region TEXT,
                    zone TEXT,
                    node_pool TEXT,
                    memory_capacity_bytes BIGINT,
                    embodied_emissions_kg REAL,
                    UNIQUE(node_name, timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_node_snapshots_timestamp ON node_snapshots(timestamp);
            """)

            # --- Table for instance_carbon_profiles ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS instance_carbon_profiles (
                    id SERIAL PRIMARY KEY,
                    provider TEXT NOT NULL,
                    instance_type TEXT NOT NULL,
                    gwp_manufacture REAL NOT NULL,
                    lifespan_hours INTEGER NOT NULL,
                    source TEXT,
                    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(provider, instance_type)
                );
                CREATE INDEX IF NOT EXISTS idx_instance_profiles_type ON instance_carbon_profiles(
                    provider, instance_type
                );
            """)

            # --- Table for recommendation_history ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS recommendation_history (
                    id SERIAL PRIMARY KEY,
                    pod_name TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    reason TEXT,
                    priority TEXT,
                    potential_savings_cost REAL,
                    potential_savings_co2e_grams REAL,
                    current_cpu_request_millicores INTEGER,
                    recommended_cpu_request_millicores INTEGER,
                    current_memory_request_bytes BIGINT,
                    recommended_memory_request_bytes BIGINT,
                    cron_schedule TEXT,
                    target_node TEXT,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reco_history_created_at
                    ON recommendation_history(created_at);
                CREATE INDEX IF NOT EXISTS idx_reco_history_type
                    ON recommendation_history(type);
            """)

            # Run versioned migrations
            runner = MigrationRunner("postgres")
            await runner.run(conn)

            logger.info("PostgreSQL schema is up to date.")


# Module-level singleton – kept for backward compatibility.
# Prefer :func:`get_db_manager` for explicit lifecycle management.
db_manager = DatabaseManager()


def get_db_manager() -> DatabaseManager:
    """Return the module-level DatabaseManager singleton.

    Using this function (rather than importing ``db_manager`` directly) makes
    it straightforward to swap or override the instance in tests and enables
    explicit lifecycle management from application entry points.
    """
    return db_manager
