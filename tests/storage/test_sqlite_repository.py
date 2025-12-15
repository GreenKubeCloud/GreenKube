# tests/storage/test_sqlite_repository.py

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.exceptions import QueryError
from greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository
from greenkube.utils.date_utils import ensure_utc, to_iso_z

# --- Fixtures ---


@pytest.fixture
def db_connection():
    """Creates an in-memory SQLite database connection for testing."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    # Create the table schema needed for the repository
    cursor.execute("""
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
    conn.commit()
    yield conn  # Provide the connection to the test
    conn.close()  # Teardown: close the connection after the test


@pytest.fixture
def mock_db_manager(db_connection):
    db_manager = MagicMock()

    @contextmanager
    def scope():
        yield db_connection

    db_manager.connection_scope = scope
    return db_manager


@pytest.fixture
def sqlite_repo(mock_db_manager):
    """Creates an instance of the SQLiteCarbonIntensityRepository."""
    return SQLiteCarbonIntensityRepository(mock_db_manager)


# --- Sample Data ---

# Consistent base time for easier reasoning
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


def test_save_history_new_records(sqlite_repo, db_connection):
    """Test saving multiple new records successfully."""
    # Act
    saved_count = sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="TEST-ZONE")

    # Assert
    assert saved_count == 3

    # Verify data in DB
    cursor = db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("TEST-ZONE",))
    assert cursor.fetchone()[0] == 3
    cursor.execute(
        "SELECT carbon_intensity FROM carbon_intensity_history WHERE datetime=?",
        (to_iso_z(ensure_utc(SAMPLE_HISTORY_DATA[1]["datetime"])),),
    )
    assert cursor.fetchone()[0] == 55.5


def test_save_history_updates_duplicates(sqlite_repo, db_connection):
    """Test that saving duplicate records (same zone and datetime) updates them."""
    # Arrange: Save initial data
    sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="DUP-ZONE")

    # Act: Try saving the same data again, plus one new record
    # Modify one of the existing records to verify update
    modified_record = SAMPLE_HISTORY_DATA[0].copy()
    modified_record["carbonIntensity"] = 999.9

    new_record = {
        "carbonIntensity": 70.0,
        "datetime": (BASE_TIME + timedelta(hours=1)).isoformat(),  # 11:00
        "updatedAt": BASE_TIME.isoformat(),
        "createdAt": BASE_TIME.isoformat(),
        "emissionFactorType": "lifecycle",
        "isEstimated": False,
        "estimationMethod": None,
    }

    # Construct input: modified existing record + other existing records + new record
    data_with_updates = [modified_record] + SAMPLE_HISTORY_DATA[1:] + [new_record]

    saved_count = sqlite_repo.save_history(data_with_updates, zone="DUP-ZONE")

    # Assert: All records are processed (updated or inserted)
    # SQLite rowcount for UPSERT is 1 per row affected
    assert saved_count == 4

    # Verify total count in DB
    cursor = db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("DUP-ZONE",))
    assert cursor.fetchone()[0] == 4  # Initial 3 + 1 new one

    # Verify the update happened
    cursor.execute(
        "SELECT carbon_intensity FROM carbon_intensity_history WHERE datetime=?",
        (to_iso_z(ensure_utc(modified_record["datetime"])),),
    )
    assert cursor.fetchone()[0] == 999.9


def test_save_history_different_zones(sqlite_repo, db_connection):
    """Test saving data for different zones."""
    # Act
    count1 = sqlite_repo.save_history(SAMPLE_HISTORY_DATA[:1], zone="ZONE-A")
    count2 = sqlite_repo.save_history(SAMPLE_HISTORY_DATA[1:], zone="ZONE-B")

    # Assert
    assert count1 == 1
    assert count2 == 2

    # Verify counts per zone
    cursor = db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("ZONE-A",))
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("ZONE-B",))
    assert cursor.fetchone()[0] == 2


def test_save_history_empty_list(sqlite_repo):
    """Test saving an empty list of records."""
    saved_count = sqlite_repo.save_history([], zone="EMPTY-ZONE")
    assert saved_count == 0


def test_save_history_invalid_record_format(sqlite_repo, db_connection):
    """Test saving data with some invalid entries (not dicts)."""
    invalid_data = [
        SAMPLE_HISTORY_DATA[0],  # Valid
        "not a dictionary",  # Invalid
        SAMPLE_HISTORY_DATA[1],  # Valid
    ]
    saved_count = sqlite_repo.save_history(invalid_data, zone="INVALID-ZONE")

    # Assert: Only valid records should be saved
    assert saved_count == 2
    cursor = db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity_history WHERE zone=?", ("INVALID-ZONE",))
    assert cursor.fetchone()[0] == 2


# --- Tests for get_for_zone_at_time ---


@pytest.mark.parametrize(
    "query_time_str, expected_intensity",
    [
        # Exact match
        (BASE_TIME.isoformat(), 60.0),  # 10:00 -> 10:00 record
        # Time between records
        (
            (BASE_TIME - timedelta(minutes=30)).isoformat(),
            55.5,
        ),  # 09:30 -> 09:00 record
        # Time exactly matching an older record
        ((BASE_TIME - timedelta(hours=1)).isoformat(), 55.5),  # 09:00 -> 09:00 record
        # Time after the latest record (should still get the latest)
        ((BASE_TIME + timedelta(hours=1)).isoformat(), 60.0),  # 11:00 -> 10:00 record
    ],
)
def test_get_for_zone_at_time_found(sqlite_repo, query_time_str, expected_intensity):
    """Test retrieving intensity for various timestamps where data exists."""
    # Arrange
    sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="GET-ZONE")

    # Act
    result = sqlite_repo.get_for_zone_at_time(zone="GET-ZONE", timestamp=query_time_str)

    # Assert
    assert result == expected_intensity


def test_get_for_zone_at_time_not_found_zone(sqlite_repo):
    """Test retrieving intensity for a zone with no data."""
    # Arrange
    sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="OTHER-ZONE")

    # Act
    result = sqlite_repo.get_for_zone_at_time(zone="NON-EXISTENT-ZONE", timestamp=BASE_TIME.isoformat())

    # Assert
    assert result is None


def test_get_for_zone_at_time_not_found_timestamp(sqlite_repo):
    """Test retrieving intensity for a time before any records exist."""
    # Arrange
    sqlite_repo.save_history(SAMPLE_HISTORY_DATA, zone="TIME-ZONE")
    query_time_str = (BASE_TIME - timedelta(hours=3)).isoformat()  # 07:00 (before 08:00 record)

    # Act
    result = sqlite_repo.get_for_zone_at_time(zone="TIME-ZONE", timestamp=query_time_str)

    # Assert
    assert result is None


def test_get_for_zone_at_time_db_error():
    """Test behavior when DB raises an error."""
    db_manager = MagicMock()

    @contextmanager
    def scope():
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = sqlite3.Error("DB Error")
        yield mock_conn

    db_manager.connection_scope = scope

    repo = SQLiteCarbonIntensityRepository(db_manager)
    with pytest.raises(QueryError):
        repo.get_for_zone_at_time(zone="ANY", timestamp=BASE_TIME.isoformat())


def test_save_history_db_error():
    """Test behavior when DB raises an error during save."""
    db_manager = MagicMock()

    @contextmanager
    def scope():
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = sqlite3.Error("DB Error")
        yield mock_conn

    db_manager.connection_scope = scope

    repo = SQLiteCarbonIntensityRepository(db_manager)
    with pytest.raises(QueryError):
        repo.save_history(SAMPLE_HISTORY_DATA, zone="ANY")
