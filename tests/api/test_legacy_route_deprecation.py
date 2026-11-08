# tests/api/test_legacy_route_deprecation.py
"""Legacy automation routes keep working and announce their removal (roadmap M4)."""

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_automation_plan_service
from greenkube.api.routers.automation import LEGACY_ROUTE_HEADERS
from greenkube.core.db import db_manager


class _NoOperations:
    async def read_operation(self, operation_id: str):
        return None


@pytest.fixture
async def client():
    await db_manager.setup_sqlite(db_path=":memory:")
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client, app
    app.dependency_overrides.clear()
    await db_manager.close()


def _assert_deprecated(response) -> None:
    for name, value in LEGACY_ROUTE_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize(
    ("method", "path", "status_code"),
    [
        ("post", "/api/v1/recommendations/1/apply-pr", 503),
        ("get", "/api/v1/recommendations/1/apply-pr/eligibility", 200),
        ("get", "/api/v1/automation/operations/op-1", 503),
    ],
)
async def test_legacy_routes_answer_with_deprecation_headers(client, method, path, status_code):
    test_client, _ = client

    response = getattr(test_client, method)(path)

    assert response.status_code == status_code
    _assert_deprecated(response)


async def test_legacy_route_errors_carry_deprecation_headers(client):
    test_client, app = client
    app.dependency_overrides[get_automation_plan_service] = lambda: _NoOperations()

    response = test_client.get("/api/v1/automation/operations/missing")

    assert response.status_code == 404
    _assert_deprecated(response)


async def test_successor_routes_are_not_marked_deprecated(client):
    test_client, _ = client

    response = test_client.get("/api/v1/automation/status")

    assert "Deprecation" not in response.headers


async def test_openapi_marks_only_the_legacy_routes_deprecated(client):
    _, app = client
    paths = app.openapi()["paths"]

    assert paths["/api/v1/recommendations/{rec_id}/apply-pr"]["post"]["deprecated"] is True
    assert paths["/api/v1/recommendations/{rec_id}/apply-pr/eligibility"]["get"]["deprecated"] is True
    assert paths["/api/v1/automation/operations/{operation_id}"]["get"]["deprecated"] is True
    assert "deprecated" not in paths["/api/v1/automation/plans"]["post"]
