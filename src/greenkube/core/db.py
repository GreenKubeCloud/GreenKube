# src/greenkube/core/db.py

import sqlite3
import psycopg2
from .config import config

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
                print("INFO: Successfully connected to SQLite database.")
                self.setup_sqlite()
            elif self.db_type == "postgres":
                self.connection = psycopg2.connect(config.DB_CONNECTION_STRING)
                print("INFO: Successfully connected to PostgreSQL database.")
                # You might need a setup_postgres() method here
            # --- AJOUT DE LA CONDITION POUR ELASTICSEARCH ---
            elif self.db_type == "elasticsearch":
                # No-op: Connection is handled by ElasticsearchCarbonIntensityRepository
                # This prevents the application from crashing on startup.
                print("INFO: DB_TYPE is 'elasticsearch'. Connection will be managed by the specific repository.")
                self.connection = None
            else:
                raise ValueError("Unsupported database type specified in config.")
        except Exception as e:
            print(f"ERROR: Could not connect to the database: {e}")
            raise

    def get_connection(self):
        return self.connection

    def close(self):
        if self.connection:
            self.connection.close()
            print("INFO: Database connection closed.")

    def setup_sqlite(self):
        """
        Creates the necessary tables for SQLite if they don't exist.
        """
        if not self.connection:
            print("ERROR: Cannot setup SQLite, no connection available.")
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
        
        self.connection.commit()
        print("INFO: SQLite schema is up to date.")

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
