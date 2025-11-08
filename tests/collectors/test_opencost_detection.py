# tests/collectors/test_opencost_detection.py
"""
Tests for OpenCost service detection (TDD first).

We expect OpenCostCollector to expose an `is_available()` method that
returns True when the OpenCost API URL responds successfully, False
when unreachable, and False when no URL is configured.
"""

import pytest
import requests
from requests_mock import ANY

from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.core.config import config as global_config


@pytest.fixture
def set_opencost_url(monkeypatch):
    # Provide a test URL via config
    monkeypatch.setattr(global_config, "OPENCOST_API_URL", "http://mock-opencost.local/api")
    yield


def test_opencost_is_available_success(set_opencost_url, requests_mock):
    oc = OpenCostCollector()

    requests_mock.get(global_config.OPENCOST_API_URL, status_code=200, json={"data": []})

    assert hasattr(oc, "is_available")
    assert callable(getattr(oc, "is_available"))

    assert oc.is_available() is True


def test_opencost_is_available_no_url(monkeypatch):
    # Clear config URL
    monkeypatch.setattr(global_config, "OPENCOST_API_URL", None)
    oc = OpenCostCollector()
    assert oc.is_available() is False


def test_opencost_is_available_connection_error(set_opencost_url, requests_mock):
    oc = OpenCostCollector()

    requests_mock.get(ANY, exc=requests.exceptions.ConnectionError("Connection refused"))

    assert oc.is_available() is False


def test_opencost_is_available_non_200(set_opencost_url, requests_mock):
    oc = OpenCostCollector()

    requests_mock.get(global_config.OPENCOST_API_URL, status_code=502, text="Bad gateway")

    assert oc.is_available() is False
