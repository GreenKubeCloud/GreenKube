import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock

from greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository


class TestSQLiteRepositoryTimezone(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        # Create table
        self.cursor.execute("""
            CREATE TABLE carbon_intensity_history (
                zone TEXT,
                carbon_intensity REAL,
                datetime TEXT,
                updated_at TEXT,
                created_at TEXT,
                emission_factor_type TEXT,
                is_estimated INTEGER,
                estimation_method TEXT,
                PRIMARY KEY (zone, datetime)
            )
        """)
        self.db_manager = MagicMock()

        @contextmanager
        def scope():
            yield self.conn

        self.db_manager.connection_scope = scope
        self.repo = SQLiteCarbonIntensityRepository(self.db_manager)

    def test_save_history_normalizes_timezone(self):
        # Arrange
        # Mixed formats: Z and +00:00
        data = [
            {"datetime": "2023-10-23T10:00:00Z", "carbonIntensity": 100},
            {"datetime": "2023-10-23T11:00:00+00:00", "carbonIntensity": 110},
        ]

        # Act
        self.repo.save_history(data, "FR")

        # Assert
        self.cursor.execute("SELECT datetime FROM carbon_intensity_history ORDER BY datetime")
        rows = self.cursor.fetchall()

        # Both should end with Z
        # Currently, the second one will be stored as +00:00
        self.assertEqual(rows[0][0], "2023-10-23T10:00:00Z")
        self.assertEqual(rows[1][0], "2023-10-23T11:00:00Z")
