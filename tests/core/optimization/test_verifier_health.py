"""Verifier health gates driven by a health collector (C0.9, BUG-35)."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.verifier import RecommendationVerifier
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.verification import KubernetesHealthObservation, WorkloadHealthObservation
from greenkube.storage.measurement_repository import SavingsMeasurementRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

from .verification_fixtures import WindowedMetricsRepo, rec_id, rows  # pyrefly: ignore[missing-import]

WINDOW = timedelta(hours=6)


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


class FakeHealthCollector:
    def __init__(self, observation):
        self.observation = observation
        self.calls: list[dict] = []

    async def collect(self, **kwargs):
        self.calls.append(kwargs)
        return self.observation


def _config() -> Config:
    return Config(
        VERIFICATION_WINDOW_HOURS=6,
        VERIFICATION_MIN_SAMPLES=12,
        VERIFICATION_MAX_RESTART_DELTA=0,
        VERIFICATION_USAGE_HEADROOM=1.1,
        VERIFICATION_MIN_READINESS=0.99,
        VERIFICATION_MAX_THROTTLE_RATIO=0.05,
    )


async def _verify(repo, observation, collector=None):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    applied_at = now - timedelta(hours=8)
    await repo.save_recommendations(
        [
            RecommendationRecord(
                pod_name="api",
                namespace="prod",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="rightsize",
                scope="workload",
                owner_kind="Deployment",
                owner_name="api",
                container_name="app",
                status=RecommendationStatus.APPLIED,
                recommended_cpu_request_millicores=300,
                potential_savings_cost=100,
                applied_at=applied_at,
                baseline={"restart_count": 0},
                verification_status="pending",
            )
        ]
    )
    metrics = WindowedMetricsRepo(
        rows(applied_at - WINDOW, applied_at, pods=("api-old-a", "api-old-b"), cost=0.01)
        + rows(applied_at, now, pods=("api-new-a", "api-new-b"), cost=0.008),
        rows(applied_at - WINDOW, applied_at, pods=("api-old-a", "api-old-b"), container="app", cost=0.01)
        + rows(applied_at, now, pods=("api-new-a", "api-new-b"), container="app", cost=0.008),
    )
    collector = collector if collector is not None else FakeHealthCollector(observation)
    verifier = RecommendationVerifier(
        RecommendationLifecycle(repo),
        metrics,  # pyrefly: ignore[bad-argument-type]
        config=_config(),
        health_collector=collector,
    )
    record = (await verifier.verify_due())[0]
    measurement = (await SavingsMeasurementRepository(db_manager).list_for_recommendation(rec_id(record)))[0]
    return record, measurement, collector, applied_at


@pytest.mark.parametrize(
    ("observation", "gate", "reason"),
    [
        (WorkloadHealthObservation(oom_kill_count=1, readiness_ratio=1.0, throttling_ratio=0.0), "oom_kills", "OOM"),
        (
            WorkloadHealthObservation(oom_kill_count=0, readiness_ratio=0.9, throttling_ratio=0.0),
            "readiness",
            "readiness",
        ),
        (
            WorkloadHealthObservation(oom_kill_count=0, readiness_ratio=1.0, throttling_ratio=0.2),
            "cpu_throttling",
            "throttling",
        ),
    ],
)
async def test_failed_health_signal_requires_rollback_review(repo, observation, gate, reason):
    record, measurement, _, _ = await _verify(repo, observation)

    assert record.status is RecommendationStatus.ROLLBACK_REVIEW
    assert any(reason in item for item in measurement.reasons)
    assert {item.gate: item.status.value for item in measurement.health}[gate] == "failed"


async def test_healthy_signals_pass_and_the_collector_gets_the_window_and_pods(repo):
    observation = WorkloadHealthObservation(oom_kill_count=0, readiness_ratio=1.0, throttling_ratio=0.01)
    record, measurement, collector, applied_at = await _verify(repo, observation)

    assert record.status is RecommendationStatus.VERIFIED
    assert all(item.status.value == "passed" for item in measurement.health)
    call = collector.calls[0]
    assert call["namespace"] == "prod"
    assert call["container_name"] == "app"
    assert sorted(call["pods"]) == ["api-new-a", "api-new-b"]
    assert call["start"] == applied_at and call["end"] == applied_at + WINDOW


async def test_missing_signals_are_reported_not_available(repo):
    observation = WorkloadHealthObservation(oom_kill_count=0, notes=["CPU CFS period series not found"])
    record, measurement, _, _ = await _verify(repo, observation)

    assert record.status is RecommendationStatus.VERIFIED
    gates = {item.gate: item for item in measurement.health}
    assert gates["oom_kills"].status.value == "passed"
    assert gates["readiness"].status.value == "not_available"
    assert gates["cpu_throttling"].status.value == "not_available"
    assert gates["cpu_throttling"].detail == "CPU CFS period series not found"


async def test_unreadable_health_is_not_available_not_passed(repo):
    class BrokenCollector(FakeHealthCollector):
        async def collect(self, **kwargs):
            raise RuntimeError("prometheus down")

    record, measurement, _, _ = await _verify(repo, None, collector=BrokenCollector(None))

    assert record.status is RecommendationStatus.VERIFIED
    gates = {item.gate: item for item in measurement.health}
    for gate in ("oom_kills", "readiness", "cpu_throttling"):
        assert gates[gate].status.value == "not_available"
        assert gates[gate].detail == "health signals could not be read"


async def test_legacy_kubernetes_observation_is_accepted(repo):
    record, measurement, _, _ = await _verify(repo, KubernetesHealthObservation(ready_pods=0, total_pods=1))

    assert record.status is RecommendationStatus.ROLLBACK_REVIEW
    assert {item.gate: item.status.value for item in measurement.health}["readiness"] == "failed"
