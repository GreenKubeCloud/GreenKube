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
    monkeypatch to set environment variables AND reloads the singleton config
    object so that *all* attributes are re-derived from the test environment,
    not just the three explicitly patched before.

    .. seealso:: BUG-003 / TEST-004 in the issue plan.
    """
    from greenkube.core.config import config
    from greenkube.core.factory import clear_caches

    # --- Core env vars for safe, isolated tests ---
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    # Avoid hitting real services
    monkeypatch.setenv("PROMETHEUS_URL", "")
    monkeypatch.setenv("OPENCOST_API_URL", "")
    monkeypatch.setenv("BOAVIZTA_API_URL", "https://api.boavizta.org")
    monkeypatch.setenv("KUBERNETES_METRICS_API_ENABLED", "false")
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "8000")
    monkeypatch.setenv("GREENKUBE_API_KEY", "")
    monkeypatch.setenv("CORS_ORIGINS", "*")
    monkeypatch.setenv("API_RATE_LIMIT", "1000/minute")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("DEFAULT_ZONE", "FR")
    monkeypatch.setenv("CLUSTER_NAME", "")
    # A local .env (e.g. PROMETHEUS_QUERY_RANGE_STEP=30m) must not change the
    # collection step the tests are written against.
    monkeypatch.setenv("PROMETHEUS_QUERY_RANGE_STEP", "5m")

    # Reload the singleton so all attributes pick up the patched env vars.
    config.reload()

    yield

    # Ensure factory caches are invalidated between tests
    clear_caches()


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


@pytest.fixture(autouse=True)
def no_live_cluster_identity(monkeypatch):
    """The savings ledger v2 never reads the kube-system UID of a live cluster in tests.

    Jobs that dual-write the ledger (savings attribution, drift monitor) build
    their own resolver; without this they would call the API server of the
    developer's kubeconfig. Tests that need the reader import it directly.
    """
    from greenkube.verify.ledger import identity

    async def _no_cluster():
        return None

    monkeypatch.setattr(identity, "read_kube_system_uid", _no_cluster)


@pytest.fixture(autouse=True)
def reset_savings_metrics_refresh_cache():
    """Make every test start with a scrape that reloads the savings gauges."""
    import sys

    module = sys.modules.get("greenkube.api.metrics_endpoint")
    if module is not None:
        module.reset_savings_refresh_cache()
    yield


@pytest.fixture(autouse=True)
def reset_evidence_history_start():
    """Warm-up gates (F7) start without a known install time: no database read, legacy window."""
    from greenkube.automation.policy import set_history_start

    set_history_start(None)
    yield
    set_history_start(None)
