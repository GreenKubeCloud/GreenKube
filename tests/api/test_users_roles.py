"""User administration and roles (roadmap F16): admin only, no lockout, disabling revokes sessions."""

from __future__ import annotations

from tests.api.auth_support import ADMIN_PASSWORD, VIEWER_PASSWORD, auth_harness  # pyrefly: ignore[missing-import]


def _users(h) -> dict[str, dict]:
    response = h.client.get("/api/v1/auth/users")
    assert response.status_code == 200, response.text
    return {user["username"]: user for user in response.json()}


def test_admin_lists_creates_and_promotes_users(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        users = _users(h)
        assert users["admin"]["role"] == "admin" and users["admin"]["auth_source"] == "local"
        assert users["viewer"]["role_managed_by_groups"] is False
        assert "password_hash" not in users["admin"]

        created = h.client.post(
            "/api/v1/auth/users",
            json={"username": "Olivia", "password": "olivia-initial-pass", "role": "operator"},
            headers=h.csrf(),
        )
        assert created.status_code == 201, created.text
        assert created.json()["username"] == "olivia" and created.json()["must_change_password"] is True

        promoted = h.client.patch(
            f"/api/v1/auth/users/{users['viewer']['id']}", json={"role": "operator"}, headers=h.csrf()
        )
        assert promoted.status_code == 200 and promoted.json()["role"] == "operator"

        h.client.cookies.clear()
        assert h.login("viewer", VIEWER_PASSWORD).json()["role"] == "operator"


def test_new_users_change_their_password_first(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        h.client.post(
            "/api/v1/auth/users", json={"username": "olivia", "password": "olivia-initial-pass"}, headers=h.csrf()
        )
        h.client.cookies.clear()
        signed_in = h.login("olivia", "olivia-initial-pass")
        assert signed_in.json()["must_change_password"] is True and signed_in.json()["role"] == "viewer"
        assert h.client.get("/api/v1/recommendations").status_code == 403


def test_weak_passwords_and_duplicate_usernames_are_refused(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        weak = h.client.post("/api/v1/auth/users", json={"username": "x", "password": "short"}, headers=h.csrf())
        assert weak.status_code == 422
        taken = h.client.post(
            "/api/v1/auth/users", json={"username": "viewer", "password": "long-enough-password"}, headers=h.csrf()
        )
        assert taken.status_code == 409


def test_the_last_active_admin_cannot_be_demoted_or_disabled(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        admin_id = _users(h)["admin"]["id"]
        demote = h.client.patch(f"/api/v1/auth/users/{admin_id}", json={"role": "viewer"}, headers=h.csrf())
        assert demote.status_code == 409 and demote.json()["detail"]["code"] == "LAST_ADMIN"
        disable = h.client.patch(f"/api/v1/auth/users/{admin_id}", json={"disabled": True}, headers=h.csrf())
        assert disable.status_code == 409


def test_disabling_a_user_revokes_its_sessions(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login("viewer", VIEWER_PASSWORD)
        viewer_cookie = h.client.cookies.get("greenkube_session")
        h.client.cookies.clear()
        h.login()
        viewer_id = _users(h)["viewer"]["id"]
        disabled = h.client.patch(f"/api/v1/auth/users/{viewer_id}", json={"disabled": True}, headers=h.csrf())
        assert disabled.status_code == 200 and disabled.json()["disabled"] is True

        h.client.cookies.clear()
        h.client.cookies.set("greenkube_session", viewer_cookie)
        assert h.client.get("/api/v1/auth/me").status_code == 401
        h.client.cookies.clear()
        assert h.login("viewer", VIEWER_PASSWORD).status_code == 401

        h.login()
        h.client.patch(f"/api/v1/auth/users/{viewer_id}", json={"disabled": False}, headers=h.csrf())
        h.client.cookies.clear()
        assert h.login("viewer", VIEWER_PASSWORD).status_code == 200


def test_user_administration_is_admin_only(tmp_path, monkeypatch):
    users = [
        ("admin", ADMIN_PASSWORD, "admin", False),
        ("operator", "operator-password-1", "operator", False),
    ]
    with auth_harness(tmp_path, monkeypatch, users=users) as h:
        h.login("operator", "operator-password-1")
        assert h.client.get("/api/v1/auth/users").status_code == 403
        created = h.client.post(
            "/api/v1/auth/users", json={"username": "x", "password": "long-enough-password"}, headers=h.csrf()
        )
        assert created.status_code == 403
        assert h.client.get("/api/v1/auth/api-keys").status_code == 403


def test_unknown_users_and_roles(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        missing = h.client.patch("/api/v1/auth/users/nope", json={"role": "viewer"}, headers=h.csrf())
        assert missing.status_code == 404
        viewer_id = _users(h)["viewer"]["id"]
        bad_role = h.client.patch(f"/api/v1/auth/users/{viewer_id}", json={"role": "root"}, headers=h.csrf())
        assert bad_role.status_code == 422
