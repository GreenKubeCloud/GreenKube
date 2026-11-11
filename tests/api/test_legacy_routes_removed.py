"""The deprecated automation compatibility routes are gone (roadmap F5, M4)."""

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.core.db import db_manager


@pytest.fixture
async def client():
    await db_manager.setup_sqlite(db_path=":memory:")
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client, app
    await db_manager.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/recommendations/1/apply-pr"),
        ("get", "/api/v1/recommendations/1/apply-pr/eligibility"),
        ("get", "/api/v1/automation/operations/op-1"),
    ],
)
async def test_legacy_routes_answer_not_found(client, method, path):
    test_client, _ = client
    response = getattr(test_client, method)(path, **({"json": {}} if method == "post" else {}))

    assert response.status_code in {404, 405}
    assert "Deprecation" not in response.headers


async def test_legacy_routes_are_absent_from_openapi(client):
    _, app = client
    paths = app.openapi()["paths"]

    assert "/api/v1/recommendations/{rec_id}/apply-pr" not in paths
    assert "/api/v1/recommendations/{rec_id}/apply-pr/eligibility" not in paths
    assert "/api/v1/automation/operations/{operation_id}" not in paths
    assert "/api/v1/recommendations/{rec_id}/pull-requests" in paths
    assert "deprecated" not in paths["/api/v1/automation/plans"]["post"]
