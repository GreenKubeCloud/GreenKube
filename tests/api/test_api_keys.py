"""Scoped API keys for machines (roadmap F15)."""

from __future__ import annotations

import re

import pytest

from tests.api.auth_support import VIEWER_PASSWORD, auth_harness  # pyrefly: ignore[missing-import]

LEGACY_KEY = "legacy-bootstrap-key"


def _create(h, name="ci", scopes=("read",), **extra):
    h.login()
    response = h.client.post(
        "/api/v1/auth/api-keys", json={"name": name, "scopes": list(scopes), **extra}, headers=h.csrf()
    )
    assert response.status_code == 201, response.text
    h.client.cookies.clear()
    return response.json()


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_a_key_is_shown_once_and_only_its_hash_is_stored(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        created = _create(h, scopes=("read", "write"))
        h.login()
        listing = h.client.get("/api/v1/auth/api-keys")
        stored = h.client.portal.call(h.service.repository.get_api_key, created["id"])

    assert re.fullmatch(r"gk_[a-z0-9]{8}_[A-Za-z0-9_-]{43}", created["key"])
    assert created["prefix"] == f"gk_{created['id']}"
    assert created["scopes"] == ["read", "write"]
    assert created["created_by"] == "user:admin"
    assert created["key"] not in listing.text
    assert listing.json()[0]["prefix"] == created["prefix"]
    secret = created["key"].split("_", 2)[2]
    assert secret not in (stored.secret_sha256, stored.prefix)


def test_a_metrics_read_key_scrapes_prometheus_but_cannot_read_the_api(tmp_path, monkeypatch):
    """F15 acceptance."""
    with auth_harness(tmp_path, monkeypatch) as h:
        key = _create(h, name="prometheus", scopes=("metrics:read",))["key"]
        scrape = h.client.get("/prometheus/metrics", headers=_bearer(key))
        recommendations = h.client.get("/api/v1/recommendations", headers=_bearer(key))

    assert scrape.status_code == 200
    assert recommendations.status_code == 403
    assert recommendations.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_read_and_write_scopes(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        read_key = _create(h, name="reader", scopes=("read",))["key"]
        read = h.client.get("/api/v1/version", headers=_bearer(read_key))
        write = h.client.delete("/api/v1/recommendations/1/ignore", headers=_bearer(read_key))
        keys = h.client.get("/api/v1/auth/api-keys", headers=_bearer(read_key))
        actor = h.client.get("/api/v1/test-support/actor", headers=_bearer(read_key))

    assert read.status_code == 200
    assert write.status_code == 403
    assert keys.status_code == 403
    assert actor.json() == {"actor": "apikey:reader"}


def test_approve_scope_is_separate_from_write(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        writer = _create(h, name="writer", scopes=("read", "write"))["key"]
        approve = h.client.post("/api/v1/automation/plans/p1/approve", json={"digest": "x"}, headers=_bearer(writer))

    assert approve.status_code == 403


def test_delivery_reports_need_the_plans_deliver_scope(tmp_path, monkeypatch):
    """A CI job reports the PR it opened with ``read`` + ``plans:deliver`` only (roadmap F4)."""
    with auth_harness(tmp_path, monkeypatch) as h:
        writer = _create(h, name="writer", scopes=("read", "write"))["key"]
        ci = _create(h, name="ci", scopes=("read", "plans:deliver"))["key"]
        body = {"pr_url": "https://github.com/acme/gitops/pull/1", "head_sha": "a" * 40}
        denied = h.client.post("/api/v1/automation/plans/p1/delivery", json=body, headers=_bearer(writer))
        allowed = h.client.post("/api/v1/automation/plans/p1/delivery", json=body, headers=_bearer(ci))
        approve = h.client.post("/api/v1/automation/plans/p1/approve", json={"digest": "x"}, headers=_bearer(ci))

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
    assert allowed.status_code not in {401, 403}, allowed.text
    assert approve.status_code == 403


def test_revoked_and_expired_keys_are_rejected(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        revoked = _create(h, name="old")
        expiring = _create(h, name="temp", expires_in_days=1)
        h.login()
        revoke = h.client.delete(f"/api/v1/auth/api-keys/{revoked['id']}", headers=h.csrf())
        again = h.client.delete(f"/api/v1/auth/api-keys/{revoked['id']}", headers=h.csrf())
        h.client.cookies.clear()
        revoked_use = h.client.get("/api/v1/version", headers=_bearer(revoked["key"]))
        valid_use = h.client.get("/api/v1/version", headers=_bearer(expiring["key"]))
        h.clock.advance(days=1, seconds=1)
        expired_use = h.client.get("/api/v1/version", headers=_bearer(expiring["key"]))

    assert revoke.status_code == 204
    assert again.status_code == 404
    assert revoked_use.status_code == 401
    assert valid_use.status_code == 200
    assert expired_use.status_code == 401


@pytest.mark.parametrize(
    "token",
    ["gk_abcdefgh_" + "x" * 43, "gk_short_secret", "not-a-key", ""],
)
def test_unknown_or_malformed_keys_are_rejected(tmp_path, monkeypatch, token):
    with auth_harness(tmp_path, monkeypatch) as h:
        response = h.client.get("/api/v1/version", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_tampered_secret_is_rejected(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        key = _create(h)["key"]
        tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
        response = h.client.get("/api/v1/version", headers=_bearer(tampered))

    assert response.status_code == 401


def test_only_admins_manage_keys(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login("viewer", VIEWER_PASSWORD)
        create = h.client.post("/api/v1/auth/api-keys", json={"name": "x", "scopes": ["admin"]}, headers=h.csrf())
        listing = h.client.get("/api/v1/auth/api-keys")

    assert create.status_code == 403
    assert listing.status_code == 403


@pytest.mark.parametrize(
    "body",
    [{"name": "x", "scopes": ["root"]}, {"name": "x", "scopes": []}, {"name": "", "scopes": ["read"]}],
)
def test_invalid_key_requests_are_rejected(tmp_path, monkeypatch, body):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        response = h.client.post("/api/v1/auth/api-keys", json=body, headers=h.csrf())

    assert response.status_code == 422


def test_legacy_bootstrap_key_still_works_as_a_deprecated_admin_key(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, GREENKUBE_API_KEY=LEGACY_KEY) as h:
        me = h.client.get("/api/v1/auth/me", headers=_bearer(LEGACY_KEY))
        actor = h.client.get("/api/v1/test-support/actor", headers=_bearer(LEGACY_KEY))
        create = h.client.post(
            "/api/v1/auth/api-keys", json={"name": "from-legacy", "scopes": ["read"]}, headers=_bearer(LEGACY_KEY)
        )

    assert me.json()["deprecated_credential"] is True
    assert "admin" in me.json()["scopes"]
    assert actor.json()["actor"].startswith("api-key:")
    assert LEGACY_KEY not in actor.text
    assert create.status_code == 201
    assert create.json()["created_by"].startswith("api-key:")


def test_api_keys_cannot_use_session_only_endpoints(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, GREENKUBE_API_KEY=LEGACY_KEY) as h:
        response = h.client.get("/api/v1/auth/sessions", headers=_bearer(LEGACY_KEY))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SESSION_REQUIRED"


def test_revoke_refuses_a_malformed_key_id_before_logging_it(tmp_path, monkeypatch, caplog):
    """CodeQL py/log-injection: a key id that cannot exist is a 404 and never reaches the log."""
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        response = h.client.delete("/api/v1/auth/api-keys/abc%0Aforged", headers=h.csrf())

    assert response.status_code == 404
    assert "forged" not in caplog.text
