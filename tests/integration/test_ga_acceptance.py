"""Release acceptance test for verified recommendation lifecycle and savings."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.db import db_manager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository


@pytest.fixture
async def acceptance_repositories():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield (
        SQLiteRecommendationRepository(db_manager),
        SQLitePullRequestRepository(db_manager),
        SQLiteSavingsLedgerRepository(db_manager),
    )
    await db_manager.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_verified_recommendation_savings_and_rollback_are_auditable(acceptance_repositories):
    recommendation_repo, _, savings_repo = acceptance_repositories
    record = RecommendationRecord(
        pod_name="checkout",
        namespace="production",
        scope="workload",
        owner_kind="Deployment",
        owner_name="checkout",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce an oversized checkout workload",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=500,
        potential_savings_cost=876.0,
        potential_savings_co2e_grams=8760.0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        patch={
            "kind": "Deployment",
            "namespace": "production",
            "name": "checkout",
            "operations": [{"op": "set_container_resources", "resource": "cpu", "field": "requests", "value": "500m"}],
        },
    )
    await recommendation_repo.save_recommendations([record])
    saved = (await recommendation_repo.get_active_recommendations())[0]
    assert saved.id is not None

    lifecycle = RecommendationLifecycle(recommendation_repo)
    applied = await lifecycle.apply(
        saved.id,
        actual_cpu=500,
        application_method="pr_merge",
        actor="acceptance",
    )
    assert applied.status == RecommendationStatus.APPLIED
    verified = await lifecycle.mark_verified(
        saved.id,
        measured_co2e_saved_grams=8000.0,
        measured_cost_saved=800.0,
        verification_window_start=applied.applied_at or datetime.now(timezone.utc),
        verification_window_end=datetime.now(timezone.utc),
    )
    assert verified.status == RecommendationStatus.VERIFIED

    attributed = await SavingsAttributor(savings_repo, "acceptance-cluster").attribute_period(
        [verified],
        period_start=datetime.now(timezone.utc) - timedelta(minutes=5),
        period_end=datetime.now(timezone.utc),
    )
    assert attributed == 1
    totals = await savings_repo.get_cumulative_totals(cluster_name="acceptance-cluster")
    assert totals["RIGHTSIZING_CPU"]["cost_saved_dollars"] > 0

    rollback_record = record.model_copy(update={"pod_name": "checkout-rollback"})
    await recommendation_repo.save_recommendations([rollback_record])
    rollback_saved = next(
        item for item in await recommendation_repo.get_active_recommendations() if item.pod_name == "checkout-rollback"
    )
    await lifecycle.apply(rollback_saved.id, actual_cpu=500, application_method="pr_merge", actor="acceptance")
    rolled_back = await lifecycle.mark_rollback_review(rollback_saved.id, reasons=["acceptance health gate"])
    assert rolled_back.status == RecommendationStatus.ROLLBACK_REVIEW
    assert (await savings_repo.get_cumulative_totals(cluster_name="acceptance-cluster"))["RIGHTSIZING_CPU"][
        "cost_saved_dollars"
    ] > 0
