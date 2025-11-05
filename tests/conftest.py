# tests/conftest.py

import sqlite3

import pytest


@pytest.fixture(scope="function")
def test_db_connection():
    """
    Pytest fixture to create and manage an in-memory SQLite database for testing.

    This fixture yields a connection to a fresh, in-memory database for each
    test function that uses it. After the test function completes, it automatically
    closes the connection.

    Using 'scope="function"' ensures that each test gets an isolated database,
    preventing side effects between tests.
    """
    # Using ":memory:" creates a temporary database in RAM
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def mock_settings_env_vars(monkeypatch):
    """
    Pytest fixture to mock environment variables for the config module.

    This fixture runs automatically for every test (`autouse=True`). It uses
    monkeypatch to set environment variables, ensuring that the application's
    config is predictable and isolated from the actual environment.
    """
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", ":memory:")  # Ensure config points to an in-memory db
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
