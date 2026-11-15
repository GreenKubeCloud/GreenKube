"""``/api/v2/changes`` (C1, C2): changes, their baseline and measurements."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.routers.changes import get_changes_repository, get_measurements_repository
from greenkube.core.db import db_manager
from greenkube.verify.model import TimeWindow
from greenkube.verify.service import DECISION_GRACE
from tests.verify.fixtures import Scenario  # pyrefly: ignore[missing-import]


@pytest.fixture
async def measured():
    """A change recorded, measured and decided on an in-memory SQLite database."""
    await db_manager.setup_sqlite(db_path=":memory:")
    scenario = Scenario(db_manager)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    change, applied = await scenario.apply()
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    yield scenario, change, applied
    await db_manager.close()


@contextmanager
def _client(scenario, api_key: str = ""):
    with patch.dict("os.environ", {"GREENKUBE_API_KEY": api_key}, clear=False):
        from greenkube.core.config import Config

        cfg = Config()
        with patch("greenkube.core.config.config", cfg), patch("greenkube.core.config.get_config", return_value=cfg):
            app = create_app()
            app.dependency_overrides[get_changes_repository] = lambda: scenario.changes
            app.dependency_overrides[get_measurements_repository] = lambda: scenario.measurements
            with TestClient(app) as client:
                yield client
            app.dependency_overrides.clear()


async def test_list_and_read_a_measured_change(measured):
    scenario, change, applied = measured
    with _client(scenario) as client:
        listing = client.get("/api/v2/changes", params={"namespace": "prod", "state": "realized"})
        assert listing.status_code == 200
        body = listing.json()
        assert body["next_cursor"] is None
        assert [item["change_id"] for item in body["items"]] == [change.change_id]
        item = body["items"][0]
        assert item["state"] == "realized"
        assert item["target"] == {"namespace": "prod", "kind": "Deployment", "name": "api"}
        assert item["recommendation_ids"] == [record.id for record in applied]
        assert item["post_window"]["start"] is not None

        assert client.get("/api/v2/changes", params={"state": "measuring"}).json()["items"] == []
        assert client.get("/api/v2/changes", params={"state": "bogus"}).status_code == 422
        assert client.get("/api/v2/changes", params={"namespace": "Bad_NS"}).status_code == 400
        assert client.get("/api/v2/changes", params={"cursor": "nope"}).status_code == 400

        detail = client.get(f"/api/v2/changes/{change.change_id}")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["baseline"]["content_verified"] is True
        assert detail_body["baseline"]["cost_covered_hours"] >= 160
        assert [event["event_type"] for event in detail_body["events"]][-1] == "measurement_decided"

        measurements = client.get(f"/api/v2/changes/{change.change_id}/measurements")
        assert measurements.status_code == 200
        payload = measurements.json()
        assert payload["state"] == "realized"
        [measurement] = payload["items"]
        assert measurement["outcome"] == "realized"
        cost = next(item for item in measurement["estimates"] if item["quantity"] == "allocated_cost")
        assert cost["claimed"] is True and cost["ci_low"] > 0
        co2e = next(item for item in measurement["estimates"] if item["quantity"] == "co2e_g")
        # Operational CO2e is claimable (component-matched), but these rows keep it constant.
        assert co2e["claimed"] is True and co2e["outcome"] != "realized"
        assert sorted(row["recommendation_id"] for row in measurement["recommendations"]) == sorted(
            record.id for record in applied
        )
        assert {row["outcome"] for row in measurement["recommendations"]} == {"realized"}
        # WP-08.1: the measured released capacity is returned with its label and units; without recorded
        # per-container requests the capacity verdict is unknown, never guessed.
        capacity = measurement["capacity"]
        assert capacity["method"] == "measured" and capacity["units"]["cpu_core_hours"] == "core-hours"
        assert capacity["verdict"] == "unknown"
        assert capacity["requests_source"]["before"] == "missing"

        assert client.get("/api/v2/changes/missing").status_code == 404
        assert client.get("/api/v2/changes/missing/measurements").status_code == 404
        assert client.get("/api/v2/changes/bad%20id").status_code == 422


async def test_change_api_requires_the_api_key(measured):
    scenario, change, _ = measured
    with _client(scenario, api_key="secret-key") as client:
        assert client.get("/api/v2/changes").status_code == 401
        assert client.get(f"/api/v2/changes/{change.change_id}/measurements").status_code == 401
        authorized = client.get("/api/v2/changes", headers={"Authorization": "Bearer secret-key"})
        assert authorized.status_code == 200 and len(authorized.json()["items"]) == 1


async def test_change_api_reports_storage_failures_as_503(measured):
    scenario, _, _ = measured

    async def broken(*args, **kwargs):
        raise ConnectionError("database down")

    with _client(scenario) as client, patch.object(scenario.changes, "list_changes", broken):
        assert client.get("/api/v2/changes").status_code == 503


async def test_list_changes_filters_by_plan_id_on_the_server(measured):
    """``/ledger?change=<plan_id>`` reads one plan's change, never a scan of recent changes (J1)."""
    scenario, change, _ = measured
    calls = []
    original = scenario.changes.list_changes

    async def recording(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    with _client(scenario) as client, patch.object(scenario.changes, "list_changes", recording):
        assert client.get("/api/v2/changes", params={"plan_id": "pln-unknown"}).json()["items"] == []
        assert calls[-1]["plan_id"] == "pln-unknown"
        assert client.get("/api/v2/changes", params={"plan_id": "bad id"}).status_code == 422
        everything = client.get("/api/v2/changes").json()["items"]
        assert [item["change_id"] for item in everything] == [change.change_id]
        assert calls[-1]["plan_id"] is None


async def test_change_pull_request_links_use_the_gitea_web_origin(measured, monkeypatch):
    """e2e: change records kept the in-cluster API host of the PR, a link no browser can open."""
    import json

    from greenkube.api.routers.changes import _change_payload

    _, change, _ = measured
    monkeypatch.setenv(
        "GITOPS_PROVIDERS_JSON",
        json.dumps(
            {
                "providers": [
                    {
                        "id": "gitea-quickstart",
                        "type": "gitea",
                        "apiBaseUrl": "http://gitea-http.gitea.svc.cluster.local:3000/api/v1",
                        "webBaseUrl": "http://localhost:3000",
                    }
                ]
            }
        ),
    )
    internal = "http://gitea-http.gitea.svc.cluster.local:3000/greenkube/repo/pulls/1"
    payload = _change_payload(change.model_copy(update={"pr_url": internal}), None)
    assert payload["pr_url"] == "http://localhost:3000/greenkube/repo/pulls/1"

    other = "https://github.com/acme/gitops/pull/7"
    assert _change_payload(change.model_copy(update={"pr_url": other}), None)["pr_url"] == other
