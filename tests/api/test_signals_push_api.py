"""POST /api/v1/signals and POST /api/v1/sources/krr/reports (roadmap G2, G6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import greenkube.api.routers.signals as signals_router
from greenkube.api.routers.signals import _json_body, get_signal_inbox
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


def _key(h, *scopes: str, name: str = "ci") -> dict:
    """A fresh API key; its name is the push client it may write (``push:<name>``)."""
    h.login()
    created = h.client.post(
        "/api/v1/auth/api-keys",
        json={"name": name, "scopes": list(scopes)},
        headers=h.csrf(),
    )
    assert created.status_code in (200, 201), created.text
    h.client.cookies.clear()
    return {"Authorization": f"Bearer {created.json()['key']}"}


def test_push_needs_the_signals_write_scope():
    assert required_scope("POST", "/api/v1/signals") == "signals:write"
    assert required_scope("POST", "/api/v1/sources/krr/reports") == "signals:report"
    assert required_scope("GET", "/api/v1/sources/krr/runs") == "read"


def test_report_uploads_have_their_own_scope(tmp_path, monkeypatch):
    """A signals:report key uploads KRR/Polaris reports and cannot push signals as a client."""
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        reporter = _key(h, "signals:report", name="krr-job")
        report = h.client.post("/api/v1/sources/krr/reports", content=KRR_FIXTURE.read_bytes(), headers=reporter)
        body = {"source_id": "push:krr-job", "complete": True, "signals": [_signal()]}
        push = h.client.post("/api/v1/signals", json=body, headers=reporter)

    assert report.status_code == 200, report.text
    assert "deprecation" not in report.headers
    assert push.status_code == 403


def test_a_signals_write_key_still_uploads_reports_with_a_deprecation_header(tmp_path, monkeypatch):
    """Existing KRR/Polaris CronJob keys keep working for one minor release."""
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        writer = _key(h, "signals:write", name="krr-job")
        report = h.client.post("/api/v1/sources/krr/reports", content=KRR_FIXTURE.read_bytes(), headers=writer)

    assert report.status_code == 200, report.text
    assert report.headers["deprecation"] == "true"
    assert "signals:report" in report.headers["warning"]


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


def test_oversized_chunked_body_is_refused_while_streaming(tmp_path, monkeypatch):
    """A body without Content-Length is read in chunks and refused past the limit (never buffered whole)."""
    monkeypatch.setattr(signals_router, "MAX_SIGNAL_BATCH_BYTES", 1024)

    def chunks():
        for _ in range(64):
            yield b" " * 512

    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        headers = {**_key(h, "signals:write"), "Content-Type": "application/json"}
        chunked = h.client.post("/api/v1/signals", content=chunks(), headers=headers)
        declared = h.client.post("/api/v1/signals", content=b" " * 2048, headers=headers)

    assert chunked.status_code == 413
    assert declared.status_code == 413
    assert inbox.signals == {}


async def test_json_body_stops_reading_at_the_limit():
    """The body is not buffered past the limit: reading stops at the first chunk beyond it."""
    received: list[int] = []

    async def receive():
        received.append(len(received))
        return {"type": "http.request", "body": b" " * 512, "more_body": len(received) < 1000}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(HTTPException) as raised:
        await _json_body(request, 1024)

    assert raised.value.status_code == 413
    assert len(received) == 3


def test_push_client_is_bound_to_the_api_key(tmp_path, monkeypatch):
    """G6: a scoped key named ``ci`` may write ``push:ci`` only; another client id is 403."""
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        ci = _key(h, "signals:write", name="ci")
        own = h.client.post(
            "/api/v1/signals", json={"source_id": "push:ci", "complete": True, "signals": [_signal()]}, headers=ci
        )
        other = h.client.post(
            "/api/v1/signals",
            json={"source_id": "push:acme-optimizer", "complete": True, "signals": [_signal()]},
            headers=ci,
        )
        named = _key(h, "signals:write", name="Acme Optimizer")
        slug = h.client.post(
            "/api/v1/signals",
            json={"source_id": "push:acme-optimizer", "complete": False, "signals": [_signal()]},
            headers=named,
        )
        invalid_other = h.client.post(
            "/api/v1/signals", json={"source_id": "push:acme-optimizer", "signals": "not-a-list"}, headers=ci
        )
        admin_key = _key(h, "admin", name="ops")
        admin_other = h.client.post(
            "/api/v1/signals",
            json={"source_id": "push:ci", "complete": False, "signals": [_signal()]},
            headers=admin_key,
        )

    assert own.status_code == 200, own.text
    assert other.status_code == 403
    assert other.json()["detail"]["code"] == "push_client_mismatch"
    assert "push:ci" in other.json()["detail"]["message"]
    assert slug.status_code == 200, slug.text
    # Another client's batch is refused before it is validated.
    assert invalid_other.status_code == 403
    # A key never writes another client's signals, whatever its scopes.
    assert admin_other.status_code == 403
    assert set(inbox.signals) and all(source in ("push:ci", "push:acme-optimizer") for source, _ in inbox.signals)
    assert [b.source_id for b in inbox.batches] == ["push:ci", "push:acme-optimizer"]


def test_signed_in_users_and_the_bootstrap_key_are_not_bound_to_one_client():
    from greenkube.api.routers.signals import push_client_of
    from greenkube.auth.principal import ANONYMOUS, Principal

    assert push_client_of(Principal(kind="apikey", name="CI pipeline #2")) == "ci-pipeline-2"
    assert push_client_of(Principal(kind="apikey", name="--")) == ""
    assert push_client_of(Principal(kind="apikey", name="legacy", legacy=True)) is None
    assert push_client_of(Principal(kind="user", name="alice", session_id="s")) is None
    assert push_client_of(ANONYMOUS) is None
    assert push_client_of(None) is None


POLARIS_FIXTURE = Path(__file__).resolve().parents[1] / "sources" / "fixtures" / "polaris_report.json"


def test_polaris_report_needs_signals_write_and_is_stored(tmp_path, monkeypatch):
    """G4: a Polaris audit pushed by a signals:write key becomes polaris hygiene signals."""
    assert required_scope("POST", "/api/v1/sources/polaris/reports") == "signals:report"
    inbox = MemoryInbox()
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_signal_inbox] = lambda: inbox
        reader = _key(h, "read", "write", name="reader")
        refused = h.client.post("/api/v1/sources/polaris/reports", content=POLARIS_FIXTURE.read_bytes(), headers=reader)
        headers = _key(h, "signals:write", name="polaris-job")
        stored = h.client.post("/api/v1/sources/polaris/reports", content=POLARIS_FIXTURE.read_bytes(), headers=headers)
        invalid = h.client.post("/api/v1/sources/polaris/reports", json={"scans": []}, headers=headers)

    assert refused.status_code == 403
    assert stored.status_code == 200, stored.text
    payload = stored.json()
    assert payload["source_id"] == "polaris" and payload["accepted"] == 5 and payload["complete"] is True
    assert {d["code"] for d in payload["diagnostics"]} >= {"passed", "ignored_severity", "invalid_entry"}
    assert {source for source, _ in inbox.signals} == {"polaris"}
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["type"] == "polaris_report"
