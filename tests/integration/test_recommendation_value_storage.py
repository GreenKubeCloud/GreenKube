# tests/integration/test_recommendation_value_storage.py
"""Recommendation ValueScore persistence (migration 0058) on SQLite and PostgreSQL."""

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.enrich import enrich_recommendations
from greenkube.models.metrics import CombinedMetric, RecommendationRecord, RecommendationType

from .test_real_database_repositories import (  # pyrefly: ignore[missing-import]  # noqa: F401
    RealDatabase,
    _migration_versions,
    real_database,
)

pytestmark = pytest.mark.database


@pytest.fixture(autouse=True)
def _no_decrease_coverage_gate(monkeypatch):
    """The fixture spans one step, not weeks: disable the one-week coverage gate for
    request decreases (E9), which tests/core/optimization/test_two_way_rightsizing.py covers."""
    monkeypatch.setenv("RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS", "0")


def _scored_recommendation():
    rows = [
        CombinedMetric(
            restart_count=0,
            pod_name="api-7f9c5d9f6d-a1b2c",
            namespace="prod",
            owner_kind="Deployment",
            owner_name="api",
            container_name="api",
            cpu_request=1000,
            cpu_usage_millicores=50,
            memory_request=1024**3,
            memory_usage_bytes=900 * 1024**2,
            duration_seconds=300,
        )
    ]
    config = Config()
    context = OptimizationContext(config=config, metrics=rows, analysis_window_seconds=3600)
    (recommendation,) = CpuRightsizingAnalyzer(config).analyze(context)
    (enriched,) = enrich_recommendations([recommendation], context, config)
    return enriched


async def test_value_round_trips_through_active_upserts(real_database: RealDatabase):  # noqa: F811
    assert 58 in await _migration_versions(real_database)
    repo = real_database.recommendation_repository()
    recommendation = _scored_recommendation()
    assert recommendation.type == RecommendationType.RIGHTSIZING_CPU
    assert recommendation.value is not None

    record = RecommendationRecord.from_recommendation(recommendation)
    assert await repo.upsert_recommendations([record]) == 1

    (stored,) = await repo.get_active_recommendations(namespace="prod")
    assert stored.value == recommendation.value
    assert stored.ranking_score == stored.value["ev"]
    assert stored.eligibility is None  # computed when read by the API, never persisted

    # Re-scans refresh the value in place.
    refreshed = dict(recommendation.value, ev=1.5)
    assert await repo.upsert_recommendations([record.model_copy(update={"value": refreshed})]) == 1
    (updated,) = await repo.get_active_recommendations(namespace="prod")
    assert updated.id == stored.id
    assert updated.value["ev"] == 1.5


async def test_records_without_value_read_back_as_none(real_database: RealDatabase):  # noqa: F811
    repo = real_database.recommendation_repository()
    record = RecommendationRecord(
        pod_name="legacy",
        namespace="prod",
        type=RecommendationType.ZOMBIE_POD,
        description="Legacy row written before the EV ranking.",
    )
    assert await repo.upsert_recommendations([record]) == 1
    (stored,) = await repo.get_active_recommendations(namespace="prod")
    assert stored.value is None
