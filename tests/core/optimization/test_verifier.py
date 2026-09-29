# tests/core/optimization/test_verifier.py
"""Tests for post-apply outcome verification and rollback review."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.verifier import RecommendationVerifier
from greenkube.models.metrics import (
    CombinedMetric,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


class FakeCombinedRepo:
    """Returns post-apply metrics on the first call and pre-apply metrics on the second."""

    def __init__(self, metrics, before_metrics=None):
        self.metrics = metrics
        self.before_metrics = before_metrics
        self.calls = 0

    async def read_combined_metrics_smart(self, start_time, end_time, namespace=None):
        self.calls += 1
        if self.before_metrics is not None and self.calls == 2:
            return list(self.before_metrics)
        return list(self.metrics)


def _config() -> Config:
    cfg = Config()
    cfg.VERIFICATION_WINDOW_HOURS = 0
    cfg.VERIFICATION_MIN_SAMPLES = 1
    return cfg


def _applied_record(
    applied_at: datetime,
    status: RecommendationStatus = RecommendationStatus.APPLIED,
    projected_cost: float = 100.0,
    projected_co2: float = 5000.0,
    restart_count: int = 0,
) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        status=status,
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        potential_savings_cost=projected_cost,
        potential_savings_co2e_grams=projected_co2,
        applied_at=applied_at,
        application_method="manual",
        baseline={
            "captured_at": applied_at.isoformat(),
            "cost_per_hour_before": 0.05,
            "co2e_grams_per_hour_before": 2.5,
            "restart_count": restart_count,
            "proposed": {"cpu_request_millicores": 300, "memory_request_bytes": None},
        },
        verification_status="pending",
    )


def _metric(
    cost: float,
    co2: float,
    cpu: float,
    restart: int = 0,
    pod_name: str = "api-abc123def",
    timestamp: datetime | None = None,
) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod_name,
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        total_cost=cost,
        co2e_grams=co2,
        cpu_usage_millicores=cpu,
        restart_count=restart,
        sample_count=1,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


class TestVerifierOutcomes:
    @pytest.mark.asyncio
    async def test_verified_when_cost_and_health_pass(self, repo):
        applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.save_recommendations([_applied_record(applied_at)])
        verifier = RecommendationVerifier(
            RecommendationLifecycle(repo),
            # After: 0.01 per 5-min sample; before: 0.05 per sample -> ~80% drop.
            FakeCombinedRepo(  # pyrefly: ignore[bad-argument-type]
                [_metric(cost=0.01, co2=0.5, cpu=200)],
                before_metrics=[_metric(cost=0.05, co2=2.5, cpu=200)],
            ),
            config=_config(),
        )

        verified = await verifier.verify_due()

        assert len(verified) == 1
        assert verified[0].status == RecommendationStatus.VERIFIED
        assert verified[0].savings_realized is True
        assert verified[0].measured_cost_saved is not None and verified[0].measured_cost_saved > 0
        assert verified[0].measured_co2e_saved_grams is not None and verified[0].measured_co2e_saved_grams > 0

    @pytest.mark.asyncio
    async def test_negligible_carbon_projection_does_not_block_verification(self, repo):
        """A sub-gram CO2e projection must not fail the carbon gate (measurement noise)."""
        applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.save_recommendations([_applied_record(applied_at, projected_co2=0.0001)])
        verifier = RecommendationVerifier(
            RecommendationLifecycle(repo),
            FakeCombinedRepo(  # pyrefly: ignore[bad-argument-type]
                [_metric(cost=0.01, co2=0.5, cpu=200)],
                before_metrics=[_metric(cost=0.05, co2=2.5, cpu=200)],
            ),
            config=_config(),
        )

        verified = await verifier.verify_due()

        assert verified[0].status == RecommendationStatus.VERIFIED

    @pytest.mark.asyncio
    async def test_rollback_review_on_restart_delta(self, repo):
        applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.save_recommendations([_applied_record(applied_at)])
        verifier = RecommendationVerifier(
            RecommendationLifecycle(repo),
            FakeCombinedRepo([_metric(cost=0.01, co2=0.5, cpu=200, restart=2)]),  # pyrefly: ignore[bad-argument-type]
            config=_config(),
        )

        updated = await verifier.verify_due()

        assert updated[0].status == RecommendationStatus.ROLLBACK_REVIEW
        assert updated[0].verification_status == "failed"

    @pytest.mark.asyncio
    async def test_rollback_review_on_cpu_headroom(self, repo):
        applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.save_recommendations([_applied_record(applied_at)])
        verifier = RecommendationVerifier(
            RecommendationLifecycle(repo),
            FakeCombinedRepo([_metric(cost=0.01, co2=0.5, cpu=500)]),  # pyrefly: ignore[bad-argument-type]
            config=_config(),
        )

        updated = await verifier.verify_due()

        assert updated[0].status == RecommendationStatus.ROLLBACK_REVIEW

    @pytest.mark.asyncio
    async def test_not_realized_when_cost_gate_fails(self, repo):
        applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.save_recommendations([_applied_record(applied_at)])
        verifier = RecommendationVerifier(
            RecommendationLifecycle(repo),
            FakeCombinedRepo([_metric(cost=0.05, co2=2.5, cpu=200)]),  # pyrefly: ignore[bad-argument-type]
            config=_config(),
        )

        updated = await verifier.verify_due()

        assert updated[0].status == RecommendationStatus.APPLIED
        assert updated[0].verification_status == "failed"
        assert updated[0].savings_realized is False

    @pytest.mark.asyncio
    async def test_inconclusive_after_extension(self, repo):
        applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.save_recommendations([_applied_record(applied_at)])
        verifier = RecommendationVerifier(RecommendationLifecycle(repo), FakeCombinedRepo([]), config=_config())  # pyrefly: ignore[bad-argument-type]

        first = await verifier.verify_due()
        assert first[0].status == RecommendationStatus.VERIFYING
        assert (first[0].baseline or {}).get("verification_extended") is True

        second = await verifier.verify_due()
        assert second[0].status == RecommendationStatus.APPLIED
        assert second[0].verification_status == "inconclusive"

    @pytest.mark.asyncio
    async def test_window_not_elapsed_is_skipped(self, repo):
        applied_at = datetime.now(timezone.utc)
        await repo.save_recommendations([_applied_record(applied_at)])
        cfg = _config()
        cfg.VERIFICATION_WINDOW_HOURS = 48
        verifier = RecommendationVerifier(
            RecommendationLifecycle(repo),
            FakeCombinedRepo([_metric(cost=0.01, co2=0.5, cpu=200)]),  # pyrefly: ignore[bad-argument-type]
            config=cfg,
        )

        assert await verifier.verify_due() == []

    @pytest.mark.asyncio
    async def test_aggregates_replicas_at_each_timestamp(self, repo):
        applied_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await repo.save_recommendations([_applied_record(applied_at)])
        first = applied_at + timedelta(minutes=10)
        second = first + timedelta(minutes=5)
        after = [
            _metric(0.01, 0.5, 100, pod_name="api-a", timestamp=first),
            _metric(0.01, 0.5, 100, pod_name="api-b", timestamp=first),
            _metric(0.01, 0.5, 100, pod_name="api-a", timestamp=second),
            _metric(0.01, 0.5, 100, pod_name="api-b", timestamp=second),
        ]
        verifier = RecommendationVerifier(
            RecommendationLifecycle(repo),
            FakeCombinedRepo(after, before_metrics=[_metric(0.05, 2.5, 100, timestamp=first)]),  # pyrefly: ignore[bad-argument-type]
            config=_config(),
        )

        updated = await verifier.verify_due()

        assert updated[0].status == RecommendationStatus.VERIFIED
        # Two replicas are summed once per timestamp, not once per sample.
        assert updated[0].measured_cost_saved is not None
        assert updated[0].measured_cost_saved > 0
