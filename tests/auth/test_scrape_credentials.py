"""Scrape token and the deprecated bootstrap key (security hardening).

The bootstrap key ``GREENKUBE_API_KEY`` used to be a non-expiring full-admin
credential structurally required for every Prometheus scrape. Scrapes now use a
dedicated ``metrics:read`` token, and ``AUTH_LEGACY_API_KEY`` restricts or disables
the bootstrap key.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.cli.controller_metrics import create_controller_metrics_app
from greenkube.cli.worker_metrics import create_worker_metrics_app
from greenkube.core.config import Config, get_config

SCRAPE_TOKEN = "scrape-token-0123456789"
LEGACY_KEY = "legacy-bootstrap-key"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _api(monkeypatch, **env) -> TestClient:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_config().reload()
    return TestClient(create_app())


def _listener_config(monkeypatch, **values):
    defaults = {
        "API_KEY": "",
        "METRICS_TOKEN": "",
        "AUTH_LEGACY_API_KEY": "admin",
        "ENVIRONMENT": "test",
        "API_AUTH_MODE": "session",
    }
    config = SimpleNamespace(**{**defaults, **values})
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: config)
    monkeypatch.setattr("greenkube.core.config.config", config)


# --------------------------------------------------------------------------- API


def test_the_scrape_token_reads_metrics_and_nothing_else(monkeypatch):
    client = _api(monkeypatch, GREENKUBE_METRICS_TOKEN=SCRAPE_TOKEN, API_AUTH_MODE="api_key")

    assert client.get("/prometheus/metrics", headers=_bearer(SCRAPE_TOKEN)).status_code == 200
    refused = client.get("/api/v1/config", headers=_bearer(SCRAPE_TOKEN))
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
    assert client.post("/api/v1/optimization/runs", headers=_bearer(SCRAPE_TOKEN)).status_code == 403
    assert client.get("/prometheus/metrics", headers=_bearer("wrong")).status_code == 401


def test_a_configured_scrape_token_closes_an_otherwise_open_development_instance(monkeypatch):
    client = _api(monkeypatch, GREENKUBE_METRICS_TOKEN=SCRAPE_TOKEN, API_AUTH_MODE="api_key")

    assert client.get("/prometheus/metrics").status_code == 401


def test_the_bootstrap_key_stays_admin_by_default_for_upgraded_installs(monkeypatch):
    client = _api(monkeypatch, GREENKUBE_API_KEY=LEGACY_KEY, API_AUTH_MODE="api_key")

    assert client.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code == 200
    assert client.get("/api/v1/version", headers=_bearer(LEGACY_KEY)).status_code == 200


def test_the_bootstrap_key_can_be_restricted_to_metrics(monkeypatch):
    client = _api(monkeypatch, GREENKUBE_API_KEY=LEGACY_KEY, AUTH_LEGACY_API_KEY="metrics")

    assert client.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code == 200
    assert client.get("/api/v1/version", headers=_bearer(LEGACY_KEY)).status_code == 403


def test_a_disabled_bootstrap_key_is_rejected(monkeypatch):
    client = _api(
        monkeypatch,
        GREENKUBE_API_KEY=LEGACY_KEY,
        GREENKUBE_METRICS_TOKEN=SCRAPE_TOKEN,
        AUTH_LEGACY_API_KEY="disabled",
    )

    assert client.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code == 401
    assert client.get("/api/v1/version", headers=_bearer(LEGACY_KEY)).status_code == 401
    assert client.get("/prometheus/metrics", headers=_bearer(SCRAPE_TOKEN)).status_code == 200


def test_api_key_mode_needs_an_admin_bootstrap_key():
    with pytest.raises(ValueError, match="AUTH_LEGACY_API_KEY=admin"):
        Config(API_AUTH_MODE="api_key", AUTH_LEGACY_API_KEY="disabled")
    with pytest.raises(ValueError, match="AUTH_LEGACY_API_KEY"):
        Config(AUTH_LEGACY_API_KEY="root")
    assert Config(AUTH_LEGACY_API_KEY=" Metrics ").AUTH_LEGACY_API_KEY == "metrics"


# --------------------------------------------------------------------------- controller and worker listeners


@pytest.mark.parametrize("factory", [create_controller_metrics_app, create_worker_metrics_app])
def test_listeners_accept_the_scrape_token(monkeypatch, factory):
    _listener_config(monkeypatch, METRICS_TOKEN=SCRAPE_TOKEN)

    with TestClient(factory()) as client:
        assert client.get("/prometheus/metrics", headers=_bearer(SCRAPE_TOKEN)).status_code == 200
        assert client.get("/prometheus/metrics", headers=_bearer("wrong")).status_code == 401
        assert client.get("/prometheus/metrics").status_code == 401


@pytest.mark.parametrize("factory", [create_controller_metrics_app, create_worker_metrics_app])
def test_listeners_accept_the_bootstrap_key_while_no_scrape_token_exists(monkeypatch, factory):
    """Without a scrape token the chart's ServiceMonitors scrape with the bootstrap key."""
    _listener_config(monkeypatch, API_KEY=LEGACY_KEY)

    with TestClient(factory()) as client:
        assert client.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code == 200


@pytest.mark.parametrize("factory", [create_controller_metrics_app, create_worker_metrics_app])
def test_listeners_stop_accepting_the_bootstrap_key_once_a_scrape_token_exists(monkeypatch, factory):
    """The chart projects the scrape token exactly when its ServiceMonitors use it (roadmap A.9).

    The admin bootstrap key then has no business on a metrics listener, where it could be guessed.
    """
    _listener_config(monkeypatch, API_KEY=LEGACY_KEY, METRICS_TOKEN=SCRAPE_TOKEN)

    with TestClient(factory()) as client:
        assert client.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code == 401
        assert client.get("/prometheus/metrics", headers=_bearer(SCRAPE_TOKEN)).status_code == 200


@pytest.mark.parametrize("factory", [create_controller_metrics_app, create_worker_metrics_app])
def test_listeners_throttle_failed_scrape_credentials_per_peer(monkeypatch, factory):
    """Failed Bearer attempts on a listener feed a per-peer budget (4x AUTH_LOGIN_RATE_LIMIT), then 429."""
    _listener_config(monkeypatch, API_KEY=LEGACY_KEY, AUTH_LOGIN_RATE_LIMIT="1/minute")
    app = factory()

    with TestClient(app, client=("203.0.113.66", 1)) as attacker, TestClient(app, client=("10.0.0.9", 1)) as scraper:
        statuses = [attacker.get("/prometheus/metrics", headers=_bearer(f"guess-{i}")).status_code for i in range(4)]
        blocked = attacker.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY))
        other_peer = scraper.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY))
        repeated = [scraper.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code for _ in range(8)]

    assert statuses == [401] * 4
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["retry-after"]) <= 60
    assert other_peer.status_code == 200
    assert repeated == [200] * 8


@pytest.mark.parametrize("factory", [create_controller_metrics_app, create_worker_metrics_app])
def test_listeners_reject_the_bootstrap_key_when_disabled(monkeypatch, factory):
    _listener_config(monkeypatch, API_KEY=LEGACY_KEY, METRICS_TOKEN=SCRAPE_TOKEN, AUTH_LEGACY_API_KEY="disabled")

    with TestClient(factory()) as client:
        assert client.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code == 401
        assert client.get("/prometheus/metrics", headers=_bearer(SCRAPE_TOKEN)).status_code == 200


def test_the_controller_loops_endpoint_needs_the_scrape_token(monkeypatch):
    _listener_config(monkeypatch, METRICS_TOKEN=SCRAPE_TOKEN)

    with TestClient(create_controller_metrics_app()) as client:
        assert client.get("/loops").status_code == 401
        assert client.get("/loops", headers=_bearer(SCRAPE_TOKEN)).status_code == 200
        assert client.get("/healthz").status_code == 200


def test_a_production_worker_without_any_scrape_credential_fails_closed(monkeypatch):
    _listener_config(monkeypatch, ENVIRONMENT="production", API_KEY=LEGACY_KEY, AUTH_LEGACY_API_KEY="disabled")

    with TestClient(create_worker_metrics_app()) as client:
        assert client.get("/prometheus/metrics", headers=_bearer(LEGACY_KEY)).status_code == 503
