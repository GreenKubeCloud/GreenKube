"""Phase 5 verification health and measurement-control tests."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.verifier import RecommendationVerifier, _Measured
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.verification import KubernetesHealthObservation


def _record() -> RecommendationRecord:
    return RecommendationRecord(
        id=1,
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="rightsize",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        status=RecommendationStatus.APPLIED,
        potential_savings_cost=100,
        potential_savings_co2e_grams=1000,
        recommended_cpu_request_millicores=300,
        baseline={
            "cost_per_hour_before": 0.05,
            "co2e_grams_per_hour_before": 2.5,
            "proposed": {"cpu_request_millicores": 300},
        },
    )


class HealthCollector:
    async def collect(self, **kwargs):
        return KubernetesHealthObservation(ready_pods=0, total_pods=1, oom_kill_count=1)


@pytest.mark.asyncio
async def test_health_failure_is_continuous_rollback_condition():
    verifier = RecommendationVerifier(
        SimpleNamespace(),  # pyrefly: ignore[bad-argument-type]
        SimpleNamespace(),  # pyrefly: ignore[bad-argument-type]
        config=Config(
            VERIFICATION_MAX_RESTART_DELTA=0,
            VERIFICATION_USAGE_HEADROOM=1.1,
            VERIFICATION_MIN_READINESS=0.99,
            VERIFICATION_MIN_SAVINGS_RATIO=0.5,
        ),
        health_collector=HealthCollector(),
    )
    measured = _Measured(
        cost_per_hour_before=0.05,
        cost_per_hour_after=0.01,
        co2e_grams_per_hour_before=2.5,
        co2e_grams_per_hour_after=0.5,
        sample_count=10,
        readiness_ratio=0.0,
        oom_kill_count=1,
    )

    outcome = verifier._evaluate(_record(), measured, datetime.now(timezone.utc), datetime.now(timezone.utc))

    assert outcome.outcome == "rollback_review"
    assert any("readiness" in reason for reason in outcome.reasons)
    assert outcome.ledger_input is not None
    assert outcome.ledger_input.sample_count == 10
