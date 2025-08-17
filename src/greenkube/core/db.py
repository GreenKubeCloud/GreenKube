# src/greenkube/core/db.py

import sqlite3
import psycopg2
from .config import config

class DatabaseManager:
    """
    Manages the connection to the database and initializes the required schema.
    Supports both SQLite and PostgreSQL based on the application's configuration.
    """

    def __init__(self):
        self.db_type = config.DB_TYPE
        self.connection = None
        self.connect()
        self.init_schema()

    def connect(self):
        """
        Establishes a connection to the configured database.
        """
        try:
            if self.db_type == "sqlite":
                self.connection = sqlite3.connect(config.DB_PATH)
                print(f"Successfully connected to SQLite database at '{config.DB_PATH}'")
            elif self.db_type == "postgres":
                self.connection = psycopg2.connect(config.DB_CONNECTION_STRING)
                print("Successfully connected to PostgreSQL database.")
            else:
                raise ValueError("Unsupported database type specified in config.")
        except (sqlite3.Error, psycopg2.Error) as e:
            print(f"Error connecting to {self.db_type} database: {e}")
            raise

    def init_schema(self):
        """
        Creates the necessary tables in the database if they don't already exist.
        This method is designed to be idempotent.
        """
        if not self.connection:
            print("Cannot initialize schema, no database connection.")
            return

        # SQL commands are written to be compatible with both SQLite and PostgreSQL
        # for key data types.
        commands = [
            """
            CREATE TABLE IF NOT EXISTS carbon_intensity (
                id SERIAL PRIMARY KEY,
                zone TEXT NOT NULL,
                carbon_intensity INTEGER NOT NULL,
                datetime TIMESTAMPTZ NOT NULL UNIQUE,
                updated_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                emission_factor_type TEXT,
                is_estimated BOOLEAN NOT NULL,
                estimation_method TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS k8s_energy_consumption (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL,
                pod_name TEXT NOT NULL,
                namespace TEXT NOT NULL,
                container_name TEXT,
                energy_kwh NUMERIC NOT NULL,
                UNIQUE (timestamp, pod_name, namespace, container_name)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS k8s_cost_allocation (
                id SERIAL PRIMARY KEY,
                window_start TIMESTAMPTZ NOT NULL,
                window_end TIMESTAMPTZ NOT NULL,
                namespace TEXT NOT NULL,
                cpu_cost NUMERIC NOT NULL,
                ram_cost NUMERIC NOT NULL,
                total_cost NUMERIC NOT NULL,
                UNIQUE (window_start, window_end, namespace)
            );
            """
        ]

        cursor = None # Initialize cursor to None
        try:
            cursor = self.connection.cursor() # Create the cursor
            for command in commands:
                # Adjust SERIAL for SQLite compatibility
                if self.db_type == 'sqlite':
                    sql_command = command.replace('SERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY AUTOINCREMENT')
                    sql_command = sql_command.replace('TIMESTAMPTZ', 'TEXT')
                else:
                    sql_command = command
                cursor.execute(sql_command)
            self.connection.commit()
            print("Database schema initialized successfully.")
        except (sqlite3.Error, psycopg2.Error) as e:
            print(f"Error initializing schema: {e}")
            self.connection.rollback()
            raise
        finally:
            if cursor:
                cursor.close()

    def get_connection(self):
        """Returns the active database connection."""
        return self.connection

# Global instance to be used across the application
db_manager = DatabaseManager()

def get_db_connection():
    """Provides easy access to the database connection."""
    return db_manager.get_connection()

