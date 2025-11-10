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


@pytest.fixture(autouse=True)
def mock_k8s_discovery(monkeypatch):
    """
    Autouse fixture to mock Kubernetes discovery helpers so tests do not
    require a live cluster. It patches BaseDiscovery methods used by the
    collectors and sets an env var to skip DNS checks during discovery.
    """
    # Patch the DNS skip env so BaseDiscovery._is_resolvable returns True in tests
    monkeypatch.setenv("GREENKUBE_DISCOVERY_SKIP_DNS_CHECK", "1")

    # Provide a conservative default: prevent attempts to load kubeconfig from disk
    # while leaving `list_services` intact so tests can patch the Kubernetes client
    # (they typically monkeypatch `greenkube.collectors.discovery.client.CoreV1Api`).
    try:
        from greenkube.collectors.discovery.base import BaseDiscovery

        def _fake_load_kube_config_quietly(self) -> bool:
            # Pretend kube config can't be loaded so BaseDiscovery will attempt to
            # instantiate client.CoreV1Api, which tests often patch.
            return False

        monkeypatch.setattr(BaseDiscovery, "_load_kube_config_quietly", _fake_load_kube_config_quietly, raising=False)
    except Exception:
        # If discovery module isn't importable, ignore — tests will patch more specifically.
        pass
