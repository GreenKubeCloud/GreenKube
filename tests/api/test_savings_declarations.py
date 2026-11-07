"""PATCH /recommendations/{id}/apply validation of user-declared savings (roadmap C0.8, BUG-34)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_recommendation_repository
from greenkube.core.db import db_manager
from greenkube.core.savings_attributor import accrual_components, user_declared_savings
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def apply_client():
    await db_manager.setup_sqlite(db_path=":memory:")
    repo = SQLiteRecommendationRepository(db_manager)
    await repo.save_recommendations(
        [
            RecommendationRecord(
                pod_name="checkout",
                namespace="prod",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="CPU oversized",
                scope="workload",
                owner_kind="Deployment",
                owner_name="checkout",
                current_cpu_request_millicores=500,
                recommended_cpu_request_millicores=250,
                potential_savings_cost=100.0,
                potential_savings_co2e_grams=1000.0,
                expires_at=datetime.now(timezone.utc) + timedelta(days=14),
            )
        ]
    )
    rec_id = (await repo.get_active_recommendations())[0].id
    app = create_app()
    app.dependency_overrides[get_recommendation_repository] = lambda: repo
    with TestClient(app) as client:
        yield client, repo, rec_id
    app.dependency_overrides.clear()
    await db_manager.close()


@pytest.mark.parametrize("body", ['{"cost_saved": NaN}', '{"carbon_saved_co2e_grams": Infinity}'])
async def test_non_finite_declarations_are_rejected(apply_client, body):
    client, repo, rec_id = apply_client

    response = client.patch(
        f"/api/v1/recommendations/{rec_id}/apply", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert (await repo.get_recommendation_by_id(rec_id)).status.value == "active"


async def test_declarations_above_ten_times_the_projection_are_rejected(apply_client):
    client, repo, rec_id = apply_client

    response = client.patch(f"/api/v1/recommendations/{rec_id}/apply", json={"cost_saved": 1001.0})

    assert response.status_code == 422
    assert "exceeds 10x" in response.json()["detail"]
    assert (await repo.get_recommendation_by_id(rec_id)).status.value == "active"


async def test_valid_declaration_is_applied_and_marked_user_declared(apply_client):
    client, repo, rec_id = apply_client

    response = client.patch(f"/api/v1/recommendations/{rec_id}/apply", json={"cost_saved": -20.0})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applied"
    assert body["baseline"]["user_declared_savings"]["cost_per_year"] == -20.0
    assert body["baseline"]["user_declared_savings"]["flags"] == ["negative_cost_saved"]

    stored = await repo.get_recommendation_by_id(rec_id)
    declared = user_declared_savings(stored)
    assert declared is not None and declared.cost_per_year == -20.0
    components = {component.method: component for component in accrual_components(stored)}
    assert components["user_declared"].cost_per_year == -20.0
    # The undeclared CO2e keeps accruing as a projection; nothing is labelled measured.
    assert components["projected"].co2e_grams_per_year == pytest.approx(1000.0)
    assert "measured" not in components


async def test_apply_without_declaration_keeps_projection(apply_client):
    client, repo, rec_id = apply_client

    response = client.patch(f"/api/v1/recommendations/{rec_id}/apply", json={})

    assert response.status_code == 200
    stored = await repo.get_recommendation_by_id(rec_id)
    assert user_declared_savings(stored) is None
    assert [component.method for component in accrual_components(stored)] == ["projected"]
