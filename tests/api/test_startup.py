# tests/api/test_startup.py
"""Tests for src/greenkube/api/startup.py — API background tasks.

Regression for BUG-21 (E1): the API must not run a recommendation scan at
startup (one scan per API replica); scans belong to the leader controller.
"""

import importlib

from greenkube.api import startup


def test_api_startup_has_no_recommendation_scan():
    """The per-replica startup scan is gone; only the controller scans."""
    assert not hasattr(startup, "run_startup_recommendation_scan")
    api_app = importlib.import_module("greenkube.api.app")
    assert not hasattr(api_app, "run_startup_recommendation_scan")


def test_heartbeat_route_is_registered_and_public():
    from fastapi.testclient import TestClient

    from greenkube.api.app import create_app

    client = TestClient(create_app())
    response = client.get("/api/v1/health/heartbeat")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_optimization_factory_dependency_is_cached():
    from greenkube.api.dependencies import get_optimization_engine
    from greenkube.core.factory import get_optimization_engine as factory_engine

    assert get_optimization_engine.__name__ == "get_optimization_engine"
    assert factory_engine.cache_info().maxsize == 1


def test_api_process_declares_no_background_loop():
    """M8: the expiry sweep no longer runs in every API replica; the controller owns it."""
    assert startup.API_BACKGROUND_LOOPS == ()
    assert not hasattr(startup, "run_plan_expiry_loop")
    api_app = importlib.import_module("greenkube.api.app")
    assert not hasattr(api_app, "run_plan_expiry_loop")
    assert not hasattr(api_app, "_inline_worker_factory")
