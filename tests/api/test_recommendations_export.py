"""Export of active recommendations with released capacity (WP-08.1)."""

import csv
import io
from unittest.mock import AsyncMock

from greenkube.models.metrics import RecommendationRecord, RecommendationType


def _record() -> RecommendationRecord:
    return RecommendationRecord(
        id=7,
        pod_name="api",
        namespace="production",
        container_name="api",
        owner_kind="Deployment",
        owner_name="api",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="=cut",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=100,
        potential_savings_cost=0.0,
        impact={
            "currency": "USD",
            "allocated_cost_annual": 236.52,
            "expected_billable_cost_annual": 0.0,
            "realization_factor": 0.0,
            "capacity_released": {
                "cpu_core_hours": 7884.0,
                "mem_gib_hours": 0.0,
                "replica_hours": 8760.0,
                "node_hours": 0.0,
            },
        },
    )


def test_csv_export_carries_released_capacity_with_units_and_the_projected_label(client, mock_reco_repo):
    mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[_record()])
    response = client.get("/api/v1/recommendations/export", params={"format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    [row] = list(csv.DictReader(io.StringIO(response.text)))
    assert row["capacity_method"] == "projected"
    assert float(row["released_cpu_cores"]) == 0.9
    assert float(row["released_cpu_core_hours_per_year"]) == 7884.0
    assert row["projected_allocated_cost_annual"] == "236.52"
    assert float(row["projected_billable_cost_annual"]) == 0.0
    # Formula injection is neutralized.
    assert row["description"] == "'=cut"


def test_json_export_carries_the_released_capacity_object(client, mock_reco_repo):
    mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[_record()])
    response = client.get("/api/v1/recommendations/export", params={"format": "json"})
    assert response.status_code == 200
    [item] = response.json()
    assert item["released_capacity"]["method"] == "projected"
    assert item["released_capacity"]["units"]["cpu_cores"] == "cores"
    assert item["projected_allocated_cost_annual"] == 236.52


def test_export_rejects_an_unknown_format(client, mock_reco_repo):
    mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[])
    assert client.get("/api/v1/recommendations/export", params={"format": "xml"}).status_code == 422
