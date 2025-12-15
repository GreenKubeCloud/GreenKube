# src/greenkube/core/db.py

import logging
import sqlite3
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

from .config import config

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages the connection to the database (SQLite or PostgreSQL).
    This class is primarily for relational database setup.
    """

    def __init__(self):
        self.db_type = config.DB_TYPE
        self.connection = None
        self.pool = None
        self.connect()

    def connect(self):
        """
        Establishes a connection to the configured database.
        """
        try:
            if self.db_type == "sqlite":
                self.connection = sqlite3.connect(config.DB_PATH)
                logger.info("Successfully connected to SQLite database.")
            elif self.db_type == "postgres":
                try:
                    self.pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=1,
                        maxconn=10,
                        dsn=config.DB_CONNECTION_STRING,
                        options=f"-c search_path={config.DB_SCHEMA}",
                    )
                    logger.info("Successfully initialized PostgreSQL connection pool.")
                    self.setup_postgres()
                except Exception as e:
                    logger.error(f"Failed to initialize PostgreSQL connection pool: {e}")
                    raise
            elif self.db_type == "elasticsearch":
                # No-op: Connection is handled by ElasticsearchCarbonIntensityRepository
                # This prevents the application from crashing on startup.
                logger.info("DB_TYPE is 'elasticsearch'. Connection will be managed by the specific repository.")
                self.connection = None
            else:
                raise ValueError("Unsupported database type specified in config.")
        except Exception as e:
            logger.error(f"Could not connect to the database: {e}")
            raise

    def get_connection(self):
        """
        Deprecated: Use connection_scope() instead.
        For SQLite, returns the single connection.
        For Postgres, this raises an error as connections should be managed via scope.
        """
        if self.db_type == "postgres":
            raise RuntimeError("For PostgreSQL, use 'with db_manager.connection_scope() as conn:'")
        self.ensure_connection()
        return self.connection

    @contextmanager
    def connection_scope(self):
        """
        Yields a database connection.
        For PostgreSQL, gets a connection from the pool and puts it back.
        For SQLite, yields the single persistent connection.
        """
        if self.db_type == "postgres":
            if not self.pool:
                self.connect()
            conn = self.pool.getconn()
            try:
                yield conn
            finally:
                self.pool.putconn(conn)
        else:
            self.ensure_connection()
            yield self.connection

    def ensure_connection(self):
        """
        Checks if the connection is alive and reconnects if necessary.
        For Postgres, checks the pool.
        """
        if self.db_type == "postgres":
            if self.pool is None or self.pool.closed:
                self.connect()
            return

        if self.connection is None:
            self.connect()
            return

        # Verify connection is alive
        try:
            self.connection.cursor().execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            logger.warning("SQLite connection was closed or invalid. Reconnecting.")
            self.connect()

    def close(self):
        if self.pool:
            self.pool.closeall()
            logger.info("PostgreSQL connection pool closed.")
        if self.connection:
            self.connection.close()
            logger.info("Database connection closed.")

    def setup_sqlite(self, db_path: str = None):
        """
        Creates the necessary tables for SQLite if they don't exist.
        """
        if db_path:
            self.connection = sqlite3.connect(db_path)

        if not self.connection:
            logger.error("Cannot setup SQLite, no connection available.")
            return

        cursor = self.connection.cursor()

        # --- Table for carbon_intensity_history ---
        cursor.execute("""
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS node_power_consumption (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                power_consumption_mw INTEGER NOT NULL,
                UNIQUE(node_name, timestamp)
            );
        """)

        # --- Table for pod_resource_usage ---
        cursor.execute("""
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
        cursor.execute("""
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
                period TEXT,
                "timestamp" TEXT,
                duration_seconds INTEGER,
                grid_intensity_timestamp TEXT,
                node_instance_type TEXT,
                node_zone TEXT,
                emaps_zone TEXT,
                is_estimated BOOLEAN,
                estimation_reasons TEXT,
                UNIQUE(pod_name, namespace, "timestamp")
            );
        """)

        # --- Table for node_snapshots ---
        cursor.execute("""
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
                UNIQUE(node_name, timestamp)
            );
        """)

        # Migrations for existing tables
        try:
            cursor.execute("ALTER TABLE combined_metrics ADD COLUMN node_instance_type TEXT")
        except sqlite3.OperationalError:
            logger.debug("Column 'node_instance_type' already exists in combined_metrics.")
        try:
            cursor.execute("ALTER TABLE combined_metrics ADD COLUMN node_zone TEXT")
        except sqlite3.OperationalError:
            logger.debug("Column 'node_zone' already exists in combined_metrics.")
        try:
            cursor.execute("ALTER TABLE combined_metrics ADD COLUMN emaps_zone TEXT")
        except sqlite3.OperationalError:
            logger.debug("Column 'emaps_zone' already exists in combined_metrics.")
        try:
            cursor.execute("ALTER TABLE combined_metrics ADD COLUMN is_estimated BOOLEAN")
        except sqlite3.OperationalError:
            logger.debug("Column 'is_estimated' already exists in combined_metrics.")
        try:
            cursor.execute("ALTER TABLE combined_metrics ADD COLUMN estimation_reasons TEXT")
        except sqlite3.OperationalError:
            logger.debug("Column 'estimation_reasons' already exists in combined_metrics.")

        self.connection.commit()
        logger.info("SQLite schema is up to date.")

    def setup_postgres(self):
        """
        Creates the necessary tables for PostgreSQL if they don't exist.
        """
        if not self.pool:
            logger.error("Cannot setup Postgres, no connection pool available.")
            return

        with self.connection_scope() as conn:
            cursor = conn.cursor()

            # Create schema if it doesn't exist
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {config.DB_SCHEMA};")

            # --- Table for carbon_intensity_history ---
            cursor.execute("""
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
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS node_power_consumption (
                    id SERIAL PRIMARY KEY,
                    node_name TEXT NOT NULL,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    power_consumption_mw INTEGER NOT NULL,
                    UNIQUE(node_name, timestamp)
                );
            """)

            # --- Table for pod_resource_usage ---
            cursor.execute("""
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
            cursor.execute("""
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
                    period TEXT,
                    timestamp TIMESTAMP WITH TIME ZONE,
                    duration_seconds INTEGER,
                    grid_intensity_timestamp TIMESTAMP WITH TIME ZONE,
                    node_instance_type TEXT,
                    node_zone TEXT,
                    emaps_zone TEXT,
                    is_estimated BOOLEAN,
                    estimation_reasons TEXT,
                    UNIQUE(pod_name, namespace, timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_combined_metrics_timestamp ON combined_metrics(timestamp);
            """)

            # --- Table for node_snapshots ---
            cursor.execute("""
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
                    UNIQUE(node_name, timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_node_snapshots_timestamp ON node_snapshots(timestamp);
            """)

            conn.commit()
        logger.info("PostgreSQL schema is up to date.")


# Singleton instance of the DatabaseManager
db_manager = DatabaseManager()


def get_db_connection():
    """
    Provides global access to the database connection object.
    """
    return db_manager.get_connection()


def close_db_connection():
    """
    Closes the global database connection.
    """
    db_manager.close()
