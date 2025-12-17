# tests/storage/test_sqlite_repository.py

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from greenkube.core.exceptions import QueryError
from greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository
from greenkube.utils.date_utils import ensure_utc, to_iso_z

# --- Fixtures ---


@pytest.fixture
async def db_connection():
    """Creates an in-memory SQLite database connection for testing."""
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute("""
            CREATE TABLE carbon_intensity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone TEXT NOT NULL,
                carbon_intensity REAL,
                datetime TEXT NOT NULL,
                updated_at TEXT,
                created_at TEXT,
                emission_factor_type TEXT,
                is_estimated BOOLEAN,
                estimation_method TEXT,
                UNIQUE(zone, datetime)
            );
        """)
        await conn.commit()
        yield conn


@pytest.fixture
async def mock_db_manager(db_connection):
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        yield db_connection

    db_manager.connection_scope = scope
    return db_manager


@pytest.fixture
async def sqlite_repo(mock_db_manager):
    """Creates an instance of the SQLiteCarbonIntensityRepository."""
    return SQLiteCarbonIntensityRepository(mock_db_manager)


# --- Sample Data ---

BASE_TIME = datetime(2025, 10, 24, 10, 0, 0, tzinfo=timezone.utc)

SAMPLE_HISTORY_DATA = [
    {
        "carbonIntensity": 50.0,
        "datetime": (BASE_TIME - timedelta(hours=2)).isoformat(),  # 08:00
        "updatedAt": BASE_TIME.isoformat(),
        "createdAt": BASE_TIME.isoformat(),
        "emissionFactorType": "lifecycle",
        "isEstimated": False,
        "estimationMethod": None,
    },
    {
        "carbonIntensity": 55.5,
        "datetime": (BASE_TIME - timedelta(hours=1)).isoformat(),  # 09:00
        "updatedAt": BASE_TIME.isoformat(),
        "createdAt": BASE_TIME.isoformat(),
        "emissionFactorType": "lifecycle",
        "isEstimated": False,
        "estimationMethod": None,
    },
    {
        "carbonIntensity": 60.0,
        "datetime": BASE_TIME.isoformat(),  # 10:00
        "updatedAt": BASE_TIME.isoformat(),
        "createdAt": BASE_TIME.isoformat(),
        "emissionFactorType": "lifecycle",
        "isEstimated": True,
        "estimationMethod": "forecast",
    },
]

# --- Test Cases ---


@pytest.mark.asyncio
async def test_save_history_new_records(sqlite_repo, db_connection):
    """Test saving multiple new records successfully."""
    saved_count = await sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="TEST-ZONE")
    assert saved_count == 3

    async with db_connection.execute(
        "SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("TEST-ZONE",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 3

    async with db_connection.execute(
        "SELECT carbon_intensity FROM carbon_intensity_history WHERE datetime=?",
        (to_iso_z(ensure_utc(SAMPLE_HISTORY_DATA[1]["datetime"])),),
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 55.5


@pytest.mark.asyncio
async def test_save_history_updates_duplicates(sqlite_repo, db_connection):
    """Test that saving duplicate records (same zone and datetime) updates them."""
    await sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="DUP-ZONE")

    modified_record = SAMPLE_HISTORY_DATA[0].copy()
    modified_record["carbonIntensity"] = 999.9

    new_record = {
        "carbonIntensity": 70.0,
        "datetime": (BASE_TIME + timedelta(hours=1)).isoformat(),
        "updatedAt": BASE_TIME.isoformat(),
        "createdAt": BASE_TIME.isoformat(),
        "emissionFactorType": "lifecycle",
        "isEstimated": False,
        "estimationMethod": None,
    }

    data_with_updates = [modified_record] + SAMPLE_HISTORY_DATA[1:] + [new_record]
    saved_count = await sqlite_repo.save_history(data_with_updates, zone="DUP-ZONE")

    assert saved_count == 4

    async with db_connection.execute(
        "SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("DUP-ZONE",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 4

    async with db_connection.execute(
        "SELECT carbon_intensity FROM carbon_intensity_history WHERE datetime=?",
        (to_iso_z(ensure_utc(modified_record["datetime"])),),
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 999.9


@pytest.mark.asyncio
async def test_save_history_different_zones(sqlite_repo, db_connection):
    """Test saving data for different zones."""
    count1 = await sqlite_repo.save_history(SAMPLE_HISTORY_DATA[:1], zone="ZONE-A")
    count2 = await sqlite_repo.save_history(SAMPLE_HISTORY_DATA[1:], zone="ZONE-B")

    assert count1 == 1
    assert count2 == 2

    async with db_connection.execute(
        "SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("ZONE-A",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 1
    async with db_connection.execute(
        "SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("ZONE-B",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 2


@pytest.mark.asyncio
async def test_save_history_empty_list(sqlite_repo):
    """Test saving an empty list of records."""
    saved_count = await sqlite_repo.save_history([], zone="EMPTY-ZONE")
    assert saved_count == 0


@pytest.mark.asyncio
async def test_save_history_invalid_record_format(sqlite_repo, db_connection):
    """Test saving data with some invalid entries (not dicts)."""
    invalid_data = [
        SAMPLE_HISTORY_DATA[0],
        "not a dictionary",
        SAMPLE_HISTORY_DATA[1],
    ]
    saved_count = await sqlite_repo.save_history(invalid_data, zone="INVALID-ZONE")

    assert saved_count == 2
    async with db_connection.execute(
        "SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("INVALID-ZONE",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_time_str, expected_intensity",
    [
        (BASE_TIME.isoformat(), 60.0),
        ((BASE_TIME - timedelta(minutes=30)).isoformat(), 55.5),
        ((BASE_TIME - timedelta(hours=1)).isoformat(), 55.5),
        ((BASE_TIME + timedelta(hours=1)).isoformat(), 60.0),
    ],
)
async def test_get_for_zone_at_time_found(sqlite_repo, query_time_str, expected_intensity):
    """Test retrieving intensity for various timestamps where data exists."""
    await sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="GET-ZONE")
    result = await sqlite_repo.get_for_zone_at_time(zone="GET-ZONE", timestamp=query_time_str)
    assert result == expected_intensity


@pytest.mark.asyncio
async def test_get_for_zone_at_time_not_found_zone(sqlite_repo):
    """Test retrieving intensity for a zone with no data."""
    await sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="OTHER-ZONE")
    result = await sqlite_repo.get_for_zone_at_time(zone="NON-EXISTENT-ZONE", timestamp=BASE_TIME.isoformat())
    assert result is None


@pytest.mark.asyncio
async def test_get_for_zone_at_time_not_found_timestamp(sqlite_repo):
    """Test retrieving intensity for a time before any records exist."""
    await sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="TIME-ZONE")
    query_time_str = (BASE_TIME - timedelta(hours=3)).isoformat()
    result = await sqlite_repo.get_for_zone_at_time(zone="TIME-ZONE", timestamp=query_time_str)
    assert result is None


@pytest.mark.asyncio
async def test_get_for_zone_at_time_db_error():
    """Test behavior when DB returns an error during get."""
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        mock_conn = MagicMock()

        # Mocking aiosqlite connection/cursor behavior for async with.
        # We simulate execute raising an exception when entering the context.
        mock_cursor_cm = MagicMock()
        mock_cursor_cm.__aenter__.side_effect = aiosqlite.Error("DB Error")
        mock_cursor_cm.__aexit__.return_value = None

        mock_conn.execute = MagicMock(return_value=mock_cursor_cm)
        yield mock_conn

    db_manager.connection_scope = scope
    repo = SQLiteCarbonIntensityRepository(db_manager)

    with pytest.raises(QueryError):
        await repo.get_for_zone_at_time(zone="ANY", timestamp=BASE_TIME.isoformat())


@pytest.mark.asyncio
async def test_save_history_db_error():
    """Test behavior when DB returns an error during save."""
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        mock_conn = MagicMock()

        async def mock_executemany(*args, **kwargs):
            raise aiosqlite.Error("DB Error")

        mock_conn.executemany = AsyncMock(side_effect=mock_executemany)
        yield mock_conn

    db_manager.connection_scope = scope
    repo = SQLiteCarbonIntensityRepository(db_manager)

    with pytest.raises(QueryError):
        await repo.save_history(SAMPLE_HISTORY_DATA, zone="ANY")
