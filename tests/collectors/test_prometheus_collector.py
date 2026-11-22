# tests/collectors/test_prometheus_collector.py
"""
Tests for the PrometheusCollector using Test-Driven Development (TDD).

We will mock all HTTP requests to the Prometheus API using respx.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from httpx import Response

from greenkube.collectors.discovery.prometheus import PrometheusDiscovery
from greenkube.collectors.prometheus_collector import PrometheusCollector, PrometheusQueryError
from greenkube.core.config import Config

# --- Mock Prometheus API Responses ---

MOCK_EMPTY_RESPONSE = {
    "status": "success",
    "data": {"resultType": "vector", "result": []},
}

MOCK_CPU_RANGE_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"namespace": "prod", "pod": "api-1", "node": "node-1"},
                "values": [[1678886400, "0.5"], [1678886700, "0.6"]],
            }
        ],
    },
}


# --- Pytest Fixtures ---


@pytest.fixture
def mock_config():
    """Fixture to create a Config instance and patch its attributes for tests."""

    config = Config()
    config.PROMETHEUS_URL = "http://mock-prometheus:9090"
    config.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    config.PROMETHEUS_VERIFY_CERTS = True
    return config


@pytest.fixture
def collector(mock_config):
    """Return a PrometheusCollector instance with a mocked config."""
    return PrometheusCollector(settings=mock_config)


# --- Test Cases ---


@pytest.mark.asyncio
@respx.mock
@patch.object(PrometheusCollector, "_discover_and_update_url")
async def test_collect_range_with_no_url_triggers_discovery(mock_discover_and_update, collector):
    """
    Test that collect_range() triggers discovery when PROMETHEUS_URL is not set.
    """
    # We need a collector with no URL
    collector.base_url = None
    discovered_url = "http://discovered-prometheus:9090"

    async def side_effect(client):
        collector.base_url = discovered_url
        return True

    mock_discover_and_update.side_effect = side_effect

    # Mock the API call to the discovered URL
    respx.get(f"{discovered_url}/api/v1/query_range").mock(return_value=Response(200, json=MOCK_CPU_RANGE_RESPONSE))

    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)

    # collect_range is async now too
    result = await collector.collect_range(start_time, end_time)

    mock_discover_and_update.assert_called_once()
    assert collector.base_url == discovered_url
    assert len(result) == 1
    assert result[0]["metric"]["pod"] == "api-1"


def test_update_url_respects_config_verify_certs(mock_config):
    """
    Test that _update_url respects the Config default for PROMETHEUS_VERIFY_CERTS.
    """
    # Create a collector with the default config (PROMETHEUS_VERIFY_CERTS = True)
    mock_config.PROMETHEUS_VERIFY_CERTS = True
    collector = PrometheusCollector(settings=mock_config)

    # Initially verify should be True (from config)
    assert collector.verify is True

    # Update URL to HTTPS - verify should remain True (from config)
    collector._update_url("https://discovered-prometheus:9090")
    assert collector.verify is True
    assert collector.base_url == "https://discovered-prometheus:9090"

    # Update URL to HTTP - verify should be False (not applicable for HTTP)
    collector._update_url("http://discovered-prometheus:9090")
    assert collector.verify is False
    assert collector.base_url == "http://discovered-prometheus:9090"

    # Test with config explicitly set to False
    mock_config.PROMETHEUS_VERIFY_CERTS = False
    collector2 = PrometheusCollector(settings=mock_config)
    collector2._update_url("https://another-prometheus:9090")
    assert collector2.verify is False


@pytest.mark.asyncio
async def test_probe_url_tries_candidate_paths_and_uses_auth_headers(collector):
    collector.bearer_token = "token"
    collector.username = "user"
    collector.password = "pass"
    bad_response = MagicMock()
    bad_response.raise_for_status.return_value = None
    bad_response.json.return_value = {"status": "error"}
    good_response = MagicMock()
    good_response.raise_for_status.return_value = None
    good_response.json.return_value = {"status": "success"}
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[bad_response, good_response])

    assert await collector._probe_url(client, "http://prometheus") is True
    assert client.get.await_args is not None
    assert client.get.await_args.kwargs["headers"] == {"Authorization": "Bearer token"}
    assert client.get.await_args.kwargs["auth"] == ("user", "pass")


@pytest.mark.asyncio
async def test_discover_and_update_url_uses_well_known_in_cluster_url(collector):
    collector._discovery._is_running_in_cluster = MagicMock(return_value=True)
    collector._probe_url = AsyncMock(side_effect=[False, True])

    with patch.object(PrometheusDiscovery, "discover", AsyncMock(return_value=None)):
        assert await collector._discover_and_update_url(AsyncMock()) is True

    assert collector.base_url == "https://prometheus-k8s.monitoring.svc.cluster.local:9090"


@pytest.mark.asyncio
async def test_collect_range_rejects_invalid_dates_and_reports_missing_prometheus(collector):
    """An unreachable or unconfigured Prometheus must never look like an empty window (A2)."""
    collector.base_url = None
    collector._discover_and_update_url = AsyncMock(return_value=False)

    with pytest.raises(ValueError):
        await collector.collect_range("not-a-date", datetime.now(timezone.utc))
    with pytest.raises(PrometheusQueryError):
        await collector.collect_range(datetime.now(timezone.utc), datetime.now(timezone.utc))


@pytest.mark.asyncio
@respx.mock
async def test_collect_range_raises_when_every_endpoint_fails(collector):
    respx.get(url__startswith=collector.base_url).mock(side_effect=httpx.ConnectError("Connection refused"))
    end_time = datetime.now(timezone.utc)

    with pytest.raises(PrometheusQueryError):
        await collector.collect_range(end_time - timedelta(minutes=5), end_time)


@pytest.mark.asyncio
@respx.mock
async def test_collect_range_raises_on_prometheus_error_status(collector):
    respx.get(url__startswith=collector.base_url).mock(
        return_value=Response(200, json={"status": "error", "error": "bad_data"})
    )
    end_time = datetime.now(timezone.utc)

    with pytest.raises(PrometheusQueryError, match="bad_data"):
        await collector.collect_range(end_time - timedelta(minutes=5), end_time)


def test_container_scoped_queries_exclude_pod_cgroup_and_pause_series(collector):
    """BUG-01, COL-09: one query reads containers and pod cgroups; pause and node-level cgroups are excluded."""
    assert 'pod!="",container!="POD"' in collector.cpu_usage_query
    assert "by (namespace,pod,container,node)" in collector.cpu_usage_query


@pytest.mark.asyncio
@respx.mock
async def test_collect_range_reads_containers_and_pod_cgroups_in_one_query(collector):
    """COL-09: no empty-result fallback; one query returns both levels."""
    route = respx.get(f"{collector.base_url}/api/v1/query_range").mock(
        return_value=Response(200, json=MOCK_CPU_RANGE_RESPONSE)
    )
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=5)

    result = await collector.collect_range(start_time, end_time)

    assert len(result) == 1
    assert result[0]["metric"]["pod"] == "api-1"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_close_closes_reusable_client(collector):
    collector._client = AsyncMock()
    collector._client.is_closed = False
    collector._client.aclose = AsyncMock()

    await collector.close()

    assert collector._client is None


@pytest.mark.asyncio
@respx.mock
async def test_collect_range_cpu_fallback_returns_empty_without_recursion(collector):
    """When the CPU fallback is also empty the collector must return [] instead of recursing."""
    respx.get(f"{collector.base_url}/api/v1/query_range").mock(return_value=Response(200, json=MOCK_EMPTY_RESPONSE))
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=5)

    assert await collector.collect_range(start_time, end_time) == []
