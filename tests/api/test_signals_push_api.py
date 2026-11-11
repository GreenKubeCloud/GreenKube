"""POST /api/v1/signals and POST /api/v1/sources/krr/reports (roadmap G2, G6)."""

from __future__ import annotations

import json
from pathlib import Path

from greenkube.api.routers.signals import get_signal_inbox
from greenkube.auth.principal import required_scope
from greenkube.signals.connectors.inbox import create_signal_inbox_repository
from tests.api.auth_support import auth_harness  # pyrefly: ignore[missing-import]
from tests.sources.fake_inbox import MemoryInbox  # pyrefly: ignore[missing-import]

KRR_FIXTURE = Path(__file__).resolve().parents[1] / "sources" / "fixtures" / "krr_report.json"


def _signal(**overrides) -> dict:
    signal = {
        "target": {"kind": "Deployment", "namespace": "shop", "name": "api", "container": "web"},
        "capability": "container_requests",
        "kind": "RIGHTSIZING_CPU",
        "proposal": {"kind": "container_resources", "container": "web", "proposed": {"cpu_request_m": 300}},
        "algorithm": {"name": "acme.p95"},
    }
    signal.update(overrides)
    return signal


def _key(h, *scopes: str) -> dict:
    h.login()
    created = h.client.post(
        "/api/v1/auth/api-keys",
        json={"name": "-".join(scopes).replace(":", "_"), "scopes": list(scopes)},
        headers=h.csrf(),
    )
    assert created.status_code in (200, 201), created.text
    h.client.cookies.clear()
    return {"Authorization": f"Bearer {created.json()['key']}"}


def test_push_needs_the_signals_write_scope():
    assert required_scope("POST", "/api/v1/signals") == "signals:write"
    assert required_scope("POST", "/api/v1/sources/krr/reports") == "signals:write"
    assert required_scope("GET", "/api/v1/sources/krr/runs") == "read"


def test_push_batch_is_stored_and_idempotent(tmp_path, monkeypatch):
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        headers = _key(h, "signals:write")
        body = {"source_id": "push:ci", "complete": True, "signals": [_signal(), _signal()]}

        first = h.client.post("/api/v1/signals", json=body, headers=headers)
        second = h.client.post("/api/v1/signals", json=body, headers=headers)

    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["source_id"] == "push:ci" and payload["accepted"] == 1 and payload["complete"] is True
    assert payload["diagnostics"][0]["code"] == "duplicate_signal"
    assert second.json()["signal_ids"] == payload["signal_ids"]
    assert len(inbox.signals) == 1
    assert inbox.batches[-1].pushed_by and inbox.batches[-1].pushed_by.startswith("apikey:")


def test_invalid_batch_returns_422_with_json_pointers(tmp_path, monkeypatch):
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        headers = _key(h, "signals:write")
        bad = _signal(proposal={"kind": "container_resources", "proposed": {"cpu_request_m": "lots"}})
        response = h.client.post(
            "/api/v1/signals",
            json={"source_id": "push:ci", "complete": False, "signals": [_signal(), bad]},
            headers=headers,
        )
        not_json = h.client.post("/api/v1/signals", content=b"{nope", headers=headers)

    assert response.status_code == 422
    pointers = [error["pointer"] for error in response.json()["errors"]]
    assert pointers == ["/signals/1/proposal/proposed/cpu_request_m"]
    assert not_json.status_code == 400
    assert inbox.signals == {}


def test_read_and_write_keys_cannot_push(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = MemoryInbox
        headers = _key(h, "read", "write")
        body = {"source_id": "push:ci", "complete": True, "signals": [_signal()]}
        response = h.client.post("/api/v1/signals", json=body, headers=headers)
        krr = h.client.post("/api/v1/sources/krr/reports", json={"scans": []}, headers=headers)

    assert response.status_code == 403
    assert krr.status_code == 403


def test_krr_report_is_parsed_and_stored(tmp_path, monkeypatch):
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        headers = _key(h, "signals:write")
        report = h.client.post("/api/v1/sources/krr/reports", content=KRR_FIXTURE.read_bytes(), headers=headers)
        partial = h.client.post(
            "/api/v1/sources/krr/reports?complete=false", json={"scans": [], "strategy": {}}, headers=headers
        )
        invalid = h.client.post("/api/v1/sources/krr/reports", json={"report": []}, headers=headers)

    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["source_id"] == "krr" and payload["accepted"] == 6
    assert {d["code"] for d in payload["diagnostics"]} >= {"not_enough_data", "unchanged"}
    assert partial.status_code == 200 and partial.json()["withdrawn"] == 0
    assert len(inbox.signals) == 6
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["type"] == "krr_report"


def test_krr_report_of_several_clusters_needs_a_cluster(tmp_path, monkeypatch):
    report = json.loads(KRR_FIXTURE.read_text())
    staging = json.loads(json.dumps(report["scans"][0]))
    staging["object"]["cluster"] = "staging"
    report["scans"].append(staging)
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        headers = _key(h, "signals:write")
        mixed = h.client.post("/api/v1/sources/krr/reports", json=report, headers=headers)
        assert inbox.signals == {}
        selected = h.client.post("/api/v1/sources/krr/reports?cluster=prod-eu", json=report, headers=headers)

    assert mixed.status_code == 422
    error = mixed.json()["errors"][0]
    assert error["pointer"] == "/scans" and error["type"] == "multiple_clusters"
    assert selected.status_code == 200, selected.text
    payload = selected.json()
    assert payload["accepted"] == 6
    assert {d["code"]: d["count"] for d in payload["diagnostics"]}["other_cluster"] == 1


def test_push_reaches_the_database_inbox(tmp_path, monkeypatch):
    """Without overrides the endpoint writes the migrated inbox tables (migration 0070)."""
    with auth_harness(tmp_path, monkeypatch) as h:
        headers = _key(h, "signals:write")
        body = {"source_id": "push:ci", "complete": True, "signals": [_signal()]}
        response = h.client.post("/api/v1/signals", content=json.dumps(body), headers=headers)
        assert response.status_code == 200, response.text
        stored = h.client.portal.call(lambda: create_signal_inbox_repository().current(source_id="push:ci"))

    assert [item.signal.signal_id for item in stored] == response.json()["signal_ids"]
