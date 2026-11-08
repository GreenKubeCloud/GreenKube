"""Routes added beside session auth resolve the principal like every /api/v1 route (F14, F15, C1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from greenkube.api.routers.changes import get_changes_repository
from tests.api.auth_support import auth_harness  # pyrefly: ignore[missing-import]


def _empty_changes_repository():
    repo = MagicMock()
    repo.list_changes = AsyncMock(return_value=[])
    return repo


def test_changes_need_a_principal_and_accept_sessions_and_read_keys(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_changes_repository] = _empty_changes_repository

        assert h.client.get("/api/v2/changes").status_code == 401

        h.login()
        signed_in = h.client.get("/api/v2/changes")
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json() == {"items": [], "next_cursor": None}

        created = h.client.post(
            "/api/v1/auth/api-keys", json={"name": "reader", "scopes": ["read"]}, headers=h.csrf()
        ).json()
        h.client.cookies.clear()
        by_key = h.client.get("/api/v2/changes", headers={"Authorization": f"Bearer {created['key']}"})
        assert by_key.status_code == 200, by_key.text

        h.login()
        metrics_only = h.client.post(
            "/api/v1/auth/api-keys", json={"name": "scraper", "scopes": ["metrics:read"]}, headers=h.csrf()
        ).json()
        h.client.cookies.clear()
        forbidden = h.client.get("/api/v2/changes", headers={"Authorization": f"Bearer {metrics_only['key']}"})
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_recommendation_lifecycle_events_record_the_signed_in_user(tmp_path, monkeypatch):
    """Ignore, restore, apply, remeasure and rollback dismissal audit the principal, not a fixed ``user``."""
    from unittest.mock import patch

    from greenkube.api.dependencies import get_recommendation_repository
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle

    seen: list[str] = []

    async def ignore(self, rec_id, *, reason=None, actor):
        seen.append(actor)
        raise ValueError("not found")

    with auth_harness(tmp_path, monkeypatch) as h, patch.object(RecommendationLifecycle, "ignore", ignore):
        h.client.app.dependency_overrides[get_recommendation_repository] = lambda: MagicMock()
        h.login()
        response = h.client.patch("/api/v1/recommendations/7/ignore", json={"reason": "kept"}, headers=h.csrf())

    assert response.status_code == 404
    assert seen == ["user:admin"]
