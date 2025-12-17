from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import aiosqlite
import pytest

from greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository


@pytest.fixture
async def db_connection():
    # Setup in-memory DB with schema
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute("""
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
        await conn.commit()
        yield conn


@pytest.mark.asyncio
async def test_save_history_normalizes_timezone(db_connection):
    # Arrange
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        yield db_connection

    db_manager.connection_scope = scope
    repo = SQLiteCarbonIntensityRepository(db_manager)

    # Mixed formats: Z and +00:00
    data = [
        {"datetime": "2023-10-23T10:00:00Z", "carbonIntensity": 100},
        {"datetime": "2023-10-23T11:00:00+00:00", "carbonIntensity": 110},
    ]

    # Act
    await repo.save_history(data, "FR")

    # Assert
    async with db_connection.execute("SELECT datetime FROM carbon_intensity_history ORDER BY datetime") as cursor:
        rows = await cursor.fetchall()

        # Both should end with Z (normalized by repo)
        # Verify the count first
        assert len(rows) == 2

        assert rows[0][0] == "2023-10-23T10:00:00Z"
        assert rows[1][0] == "2023-10-23T11:00:00Z"
