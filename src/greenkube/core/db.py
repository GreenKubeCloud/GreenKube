# src/greenkube/core/db.py

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import asyncpg

from .config import config

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages the asynchronous connection to the database (SQLite or PostgreSQL).
    """

    def __init__(self):
        self.connection = None
        self.pool = None
        self._lock = asyncio.Lock()

    @property
    def db_type(self):
        return config.DB_TYPE

    async def connect(self):
        """
        Establishes a connection to the configured database.
        """
        async with self._lock:
            if self.connection or self.pool:
                return

            try:
                if self.db_type == "sqlite":
                    self.connection = await aiosqlite.connect(config.DB_PATH)
                    self.connection.row_factory = aiosqlite.Row
                    logger.info("Successfully connected to SQLite database.")
                    await self.setup_sqlite()
                elif self.db_type == "postgres":
                    try:
                        self.pool = await asyncpg.create_pool(
                            dsn=config.DB_CONNECTION_STRING,
                            min_size=1,
                            max_size=10,
                            server_settings={"search_path": config.DB_SCHEMA},
                        )
                        logger.info("Successfully initialized PostgreSQL connection pool.")
                        await self.setup_postgres()
                    except Exception as e:
                        logger.error(f"Failed to initialize PostgreSQL connection pool: {e}")
                        raise
                elif self.db_type == "elasticsearch":
                    # No-op: Connection is handled by ElasticsearchCarbonIntensityRepository
                    logger.info("DB_TYPE is 'elasticsearch'. Connection will be managed by the specific repository.")
                    self.connection = None
                else:
                    raise ValueError("Unsupported database type specified in config.")
            except Exception as e:
                logger.error(f"Could not connect to the database: {e}")
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
            # --- Table for carbon_intensity_history ---
            await self.connection.execute("""
                CREATE TABLE IF NOT EXISTS carbon_intensity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone TEXT NOT NULL,
                    carbon_intensity INTEGER NOT NULL,
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
                    node_instance_type TEXT,
                    node_zone TEXT,
                    emaps_zone TEXT,
                    is_estimated BOOLEAN,
                    estimation_reasons TEXT,
                    embodied_co2e_grams REAL,
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

            # Migrations for existing tables
            # aiosqlite executes raise sqlite3.OperationalError
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN node_instance_type TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN node_zone TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN emaps_zone TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN is_estimated BOOLEAN")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN estimation_reasons TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN embodied_co2e_grams REAL")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN cpu_usage_millicores INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN memory_usage_bytes INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN owner_kind TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE combined_metrics ADD COLUMN owner_name TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                await self.connection.execute("ALTER TABLE node_snapshots ADD COLUMN embodied_emissions_kg REAL")
            except sqlite3.OperationalError:
                pass
            # Extended resource metrics columns
            for col_name, col_type in [
                ("network_receive_bytes", "REAL"),
                ("network_transmit_bytes", "REAL"),
                ("disk_read_bytes", "REAL"),
                ("disk_write_bytes", "REAL"),
                ("storage_request_bytes", "INTEGER"),
                ("storage_usage_bytes", "INTEGER"),
                ("ephemeral_storage_request_bytes", "INTEGER"),
                ("ephemeral_storage_usage_bytes", "INTEGER"),
                ("gpu_usage_millicores", "INTEGER"),
                ("restart_count", "INTEGER"),
            ]:
                try:
                    await self.connection.execute(f"ALTER TABLE combined_metrics ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass

            await self.connection.commit()
            logger.info("SQLite schema is up to date.")
        except Exception as e:
            logger.error(f"Error checking/creating SQLite tables: {e}")

    async def setup_postgres(self):
        """
        Creates the necessary tables for PostgreSQL if they don't exist.
        """
        if not self.pool:
            logger.error("Cannot setup Postgres, no connection pool available.")
            return

        async with self.connection_scope() as conn:
            # Create schema if it doesn't exist
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {config.DB_SCHEMA};")

            # --- Table for carbon_intensity_history ---
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS carbon_intensity_history (
                    id SERIAL PRIMARY KEY,
                    zone TEXT NOT NULL,
                    carbon_intensity INTEGER NOT NULL,
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
                    node_instance_type TEXT,
                    node_zone TEXT,
                    emaps_zone TEXT,
                    is_estimated BOOLEAN,
                    estimation_reasons TEXT,
                    embodied_co2e_grams REAL,
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

            # --- Migrations ---
            # Use 'ADD COLUMN IF NOT EXISTS' which works in Postgres 9.6+
            try:
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node_instance_type TEXT;")
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS node_zone TEXT;")
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS emaps_zone TEXT;")
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS is_estimated BOOLEAN;")
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS estimation_reasons TEXT;")
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS embodied_co2e_grams REAL DEFAULT 0.0;"
                )
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS cpu_usage_millicores INTEGER;"
                )
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS memory_usage_bytes BIGINT;")
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS owner_kind TEXT;")
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS owner_name TEXT;")

                # Fix any existing NULLs for non-nullable fields or fields where we expect a default
                await conn.execute(
                    "UPDATE combined_metrics SET embodied_co2e_grams = 0.0 WHERE embodied_co2e_grams IS NULL;"
                )

                await conn.execute("ALTER TABLE node_snapshots ADD COLUMN IF NOT EXISTS embodied_emissions_kg REAL;")

                # Extended resource metrics columns
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS network_receive_bytes DOUBLE PRECISION;"
                )
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS network_transmit_bytes DOUBLE PRECISION;"
                )
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS disk_read_bytes DOUBLE PRECISION;"
                )
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS disk_write_bytes DOUBLE PRECISION;"
                )
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS storage_request_bytes BIGINT;"
                )
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS storage_usage_bytes BIGINT;")
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS ephemeral_storage_request_bytes BIGINT;"
                )
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS ephemeral_storage_usage_bytes BIGINT;"
                )
                await conn.execute(
                    "ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS gpu_usage_millicores INTEGER;"
                )
                await conn.execute("ALTER TABLE combined_metrics ADD COLUMN IF NOT EXISTS restart_count INTEGER;")
            except Exception as e:
                logger.warning(f"Migration warning (non-critical): {e}")

            # No commit() needed for asyncpg (autocommit is default in some contexts).
            # asyncpg connection usage usually auto-commits if not in transaction.
            # But here we are using connection_scope which yields a connection from pool.
            # It does not start a transaction by default. So execute is autocommit.
            logger.info("PostgreSQL schema is up to date.")


# Singleton instance of the DatabaseManager
db_manager = DatabaseManager()
