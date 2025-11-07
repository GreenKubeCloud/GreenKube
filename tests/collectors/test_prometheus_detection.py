# tests/collectors/test_prometheus_detection.py
"""
Tests for Prometheus service detection (TDD first).

We expect PrometheusCollector to expose an `is_available()` method that
returns True when Prometheus endpoints respond successfully, and False
when no reachable endpoint is found or when no URL is configured.

These tests mock HTTP responses and network errors.
"""

import pytest
import requests
from requests_mock import ANY

from greenkube.collectors.prometheus_collector import PrometheusCollector
from greenkube.core.config import Config


@pytest.fixture
def mock_config():
    cfg = Config()
    cfg.PROMETHEUS_URL = "http://mock-prometheus:9090"
    cfg.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    return cfg


def test_is_available_success(mock_config, requests_mock):
    collector = PrometheusCollector(settings=mock_config)

    # Mock the primary candidate endpoint to return a successful response
    # Use a simple 200 OK with JSON that indicates success
    health_url = f"{collector.base_url.rstrip('/')}/api/v1/query"
    requests_mock.get(health_url, json={"status": "success", "data": {"result": []}})

    assert hasattr(collector, "is_available")
    assert callable(getattr(collector, "is_available"))

    assert collector.is_available() is True


def test_is_available_no_url_configured():
    cfg = Config()
    cfg.PROMETHEUS_URL = None
    cfg.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    collector = PrometheusCollector(settings=cfg)

    # With no base URL, is_available should return False
    assert collector.is_available() is False


def test_is_available_connection_error(mock_config, requests_mock):
    collector = PrometheusCollector(settings=mock_config)

    # Make all requests raise a connection error
    requests_mock.get(ANY, exc=requests.exceptions.ConnectionError("Connection refused"))

    assert collector.is_available() is False


def test_is_available_non_success_status(mock_config, requests_mock):
    collector = PrometheusCollector(settings=mock_config)

    # Return non-success JSON payload
    health_url = f"{collector.base_url.rstrip('/')}/api/v1/query"
    requests_mock.get(health_url, status_code=500, json={"status": "error"})

    assert collector.is_available() is False
