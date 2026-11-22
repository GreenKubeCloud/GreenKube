"""GET /api/v1/automation/status reports the controller loop state from persisted heartbeats (M8)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.routers.automation import get_controller_heartbeats
from greenkube.storage.controller_heartbeat_repository import ControllerHeartbeat

_LOOPS = [
    {"name": "automation-controller", "state": "failing", "ready": True, "consecutive_failures": 3},
    {"name": "collection", "state": "idle", "ready": True, "consecutive_failures": 0},
]


class Heartbeats:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error

    async def list_since(self, since):
        if self.error is not None:
            raise self.error
        return [row for row in self.rows if row.heartbeat_at >= since]


@pytest.fixture
def client_with():
    app = create_app()

    def build(repository):
        app.dependency_overrides[get_controller_heartbeats] = lambda: repository
        return TestClient(app)

    yield build
    app.dependency_overrides.clear()


def test_status_reports_the_fresh_leader_and_its_loops(client_with):
    now = datetime.now(timezone.utc)
    rows = [
        ControllerHeartbeat("controller-a", "controller", True, True, now - timedelta(seconds=20), now, _LOOPS),
        ControllerHeartbeat("controller-b", "controller", False, True, now - timedelta(seconds=25), now, []),
        ControllerHeartbeat("controller-old", "controller", True, True, now - timedelta(hours=2), now, []),
    ]
    with client_with(Heartbeats(rows)) as client:
        body = client.get("/api/v1/automation/status").json()

    controller = body["controller"]
    assert controller["available"] is True
    assert controller["healthy"] is True and controller["leader"] == "controller-a"
    by_id = {item["instance_id"]: item for item in controller["instances"]}
    assert set(by_id) == {"controller-a", "controller-b", "controller-old"}
    assert by_id["controller-old"]["stale"] is True and by_id["controller-old"]["ready"] is False
    assert by_id["controller-a"]["loops"][0]["consecutive_failures"] == 3


def test_status_has_no_healthy_controller_without_a_fresh_leader(client_with):
    now = datetime.now(timezone.utc)
    rows = [
        ControllerHeartbeat("controller-old", "controller", True, True, now - timedelta(minutes=10), now, []),
        ControllerHeartbeat("controller-b", "controller", False, True, now - timedelta(seconds=5), now, []),
    ]
    with client_with(Heartbeats(rows)) as client:
        controller = client.get("/api/v1/automation/status").json()["controller"]
    assert controller["healthy"] is False and controller["leader"] is None


def test_status_survives_an_unreadable_heartbeat_table(client_with):
    with client_with(Heartbeats(error=RuntimeError("no such table"))) as client:
        response = client.get("/api/v1/automation/status")
    assert response.status_code == 200
    assert response.json()["controller"] == {
        "available": False,
        "healthy": False,
        "leader": None,
        "stale_after_seconds": 120.0,
        "instances": [],
    }
