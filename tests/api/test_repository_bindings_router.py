"""The legacy repository-binding API is read-only (security hardening)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.routers.repository_bindings import router


def test_no_route_writes_repository_bindings():
    """POST /repository-bindings stored any repo_url for any workload; nothing used it."""
    assert {method for route in router.routes for method in getattr(route, "methods", set())} <= {"GET", "HEAD"}


def test_post_repository_bindings_is_not_routed():
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/repository-bindings",
        json={
            "cluster": "c",
            "namespace": "shop",
            "workload_kind": "Deployment",
            "workload_name": "api",
            "repo_url": "https://attacker.example/repo.git",
        },
    )

    assert response.status_code == 405
