# src/greenkube/core/db.py

import logging
import sqlite3

import psycopg2

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
                self.connection = psycopg2.connect(config.DB_CONNECTION_STRING)
                logger.info("Successfully connected to PostgreSQL database.")
                self.setup_postgres()
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
        return self.connection

    def close(self):
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
            pass
        try:
            cursor.execute("ALTER TABLE combined_metrics ADD COLUMN node_zone TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE combined_metrics ADD COLUMN emaps_zone TEXT")
        except sqlite3.OperationalError:
            pass

        self.connection.commit()
        logger.info("SQLite schema is up to date.")

    def setup_postgres(self):
        """
        Creates the necessary tables for PostgreSQL if they don't exist.
        """
        if not self.connection:
            logger.error("Cannot setup Postgres, no connection available.")
            return

        cursor = self.connection.cursor()

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
                UNIQUE(pod_name, namespace, timestamp)
            );
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
        """)

        self.connection.commit()
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
