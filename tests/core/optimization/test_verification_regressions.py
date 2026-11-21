# tests/core/optimization/test_verification_regressions.py
"""End-to-end verifier regressions ported from the audit scripts.

``verify_e2e.py`` and ``verify_container.py`` wrote five-minute metrics into a
real SQLite database, compressed them with :class:`MetricsCompressor`, and ran
the verifier over an applied CPU rightsizing recommendation. They reproduced
BUG-27 (traffic normalization inflated savings ~4x), BUG-28 (replica-summed
p95 sent multi-replica changes to rollback review), BUG-29 (container-scoped
changes were never measurable) and BUG-31 (``not_realized`` was re-tested
every few hours with a growing window).

Workload: constant usage, CPU request cut 1000m -> 300m. Request-based cost
drops 20% per replica; usage-based CO2e does not change.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.core.optimization import verifier as verifier_module
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.verifier import RecommendationVerifier
from greenkube.models.metrics import (
    CombinedMetric,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

HOURS_PER_YEAR = 8760.0
BEFORE_COST = 0.01  # per 5-minute sample and replica
AFTER_COST = 0.008  # -20% after the CPU request cut
TRUE_COST_SAVING_PER_REPLICA = (BEFORE_COST - AFTER_COST) * 12 * HOURS_PER_YEAR


@pytest.fixture
async def database():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield db_manager
    await db_manager.close()


def _metrics(
    now: datetime,
    applied_at: datetime,
    *,
    replicas: int,
    traffic: bool,
    container: bool,
    after_cost: float,
    cpu_usage: int,
) -> list[CombinedMetric]:
    out: list[CombinedMetric] = []
    t = applied_at - timedelta(hours=80)
    t = t.replace(second=0, microsecond=0, minute=(t.minute // 5) * 5)
    while t < now:
        after = t >= applied_at
        for replica in "abcdef"[:replicas]:
            pod = CombinedMetric(
                pod_name=f"api-{'new' if after else 'old'}-{replica}",
                namespace="prod",
                owner_kind="Deployment",
                owner_name="api",
                total_cost=after_cost if after else BEFORE_COST,
                co2e_grams=1.0,
                cpu_request=300 if after else 1000,
                cpu_usage_millicores=cpu_usage,
                network_receive_bytes=1000.0 if traffic else None,
                network_transmit_bytes=1000.0 if traffic else None,
                restart_count=0,
                timestamp=t,
                duration_seconds=300,
                is_estimated=False,
            )
            out.append(pod)
            if container:
                # Container rows are blanket-flagged as estimated today (BUG-29).
                out.append(pod.model_copy(update={"container_name": "app", "is_estimated": True}))
        t += timedelta(minutes=5)
    return out


async def _scenario(
    database,
    *,
    replicas: int,
    traffic: bool = False,
    container: bool = False,
    after_cost: float = AFTER_COST,
    cpu_usage: int = 200,
):
    cfg = Config()
    now = datetime.now(timezone.utc)
    applied_at = now - timedelta(hours=80)
    combined = SQLiteCombinedMetricsRepository(database)
    await combined.write_combined_metrics(
        _metrics(
            now,
            applied_at,
            replicas=replicas,
            traffic=traffic,
            container=container,
            after_cost=after_cost,
            cpu_usage=cpu_usage,
        )
    )
    await MetricsCompressor(database, cfg, collection_watermark_provider=None).run()

    annual_cost = 0.12 * replicas * HOURS_PER_YEAR
    annual_co2 = 12 * replicas * HOURS_PER_YEAR
    reco = SQLiteRecommendationRepository(database)
    await reco.save_recommendations(
        [
            RecommendationRecord(
                pod_name="api",
                namespace="prod",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="CPU oversized",
                scope="workload",
                owner_kind="Deployment",
                owner_name="api",
                container_name="app" if container else None,
                status=RecommendationStatus.APPLIED,
                current_cpu_request_millicores=1000,
                recommended_cpu_request_millicores=300,
                # The legacy projection applies the request-cut ratio to the whole pod cost (BUG-15/16).
                potential_savings_cost=annual_cost * 0.7,
                potential_savings_co2e_grams=annual_co2 * 0.7,
                carbon_saved_co2e_grams=annual_co2 * 0.7,
                cost_saved=annual_cost * 0.7,
                applied_at=applied_at,
                application_method="detected",
                baseline={"restart_count": 0, "proposed": {"cpu_request_millicores": 300}},
                verification_status="pending",
            )
        ]
    )
    verifier = RecommendationVerifier(RecommendationLifecycle(reco), combined, config=cfg)
    outcomes = await verifier.verify_due()
    record = (await reco.get_recommendations_by_statuses([status.value for status in RecommendationStatus]))[0]
    return reco, verifier, outcomes, record


class TestAuditVerifyE2E:
    async def test_single_replica_cpu_cut_realizes_cost_and_does_not_claim_co2e(self, database):
        _, _, outcomes, record = await _scenario(database, replicas=1)

        assert [outcome.status for outcome in outcomes] == [RecommendationStatus.VERIFIED]
        assert record.status is RecommendationStatus.VERIFIED
        assert record.measured_cost_saved == pytest.approx(TRUE_COST_SAVING_PER_REPLICA, rel=0.05)
        # Usage-based CO2e did not change: rightsizing never claims a carbon saving.
        assert record.measured_co2e_saved_grams == 0.0

    async def test_traffic_does_not_inflate_measured_savings(self, database):
        # 100m per replica keeps the old replica-summed p95 under the gate, isolating BUG-27.
        _, _, _, record = await _scenario(database, replicas=2, traffic=True, cpu_usage=100)

        assert record.status is RecommendationStatus.VERIFIED
        assert record.measured_cost_saved == pytest.approx(2 * TRUE_COST_SAVING_PER_REPLICA, rel=0.05)
        assert record.measured_co2e_saved_grams == 0.0

    async def test_two_replicas_within_their_per_pod_request_do_not_need_rollback_review(self, database):
        # 2 replicas at 200m with 300m proposed: the replica sum (400m) is not a per-pod usage.
        _, _, _, record = await _scenario(database, replicas=2, cpu_usage=200)

        assert record.status is not RecommendationStatus.ROLLBACK_REVIEW
        assert record.status is RecommendationStatus.VERIFIED

    async def test_not_realized_is_terminal_for_the_measurement(self, database, monkeypatch):
        # A clear cost increase: equal costs can straddle the noise floor once compression jitters the rate.
        reco, verifier, _, record = await _scenario(database, replicas=1, after_cost=BEFORE_COST * 1.2)
        assert record.status is RecommendationStatus.APPLIED
        assert record.verification_status == "failed"
        assert record.id is not None
        events_before = await reco.get_events(record.id)

        class Shifted(datetime):
            @classmethod
            def now(cls, tz=None):  # pyrefly: ignore[bad-override]
                return datetime.now(tz) + timedelta(hours=8)

        monkeypatch.setattr(verifier_module, "datetime", Shifted)
        again = await verifier.verify_due()

        assert again == []
        assert len(await reco.get_events(record.id)) == len(events_before)


class TestAuditVerifyContainer:
    @pytest.mark.parametrize("replicas", [1, 2])
    async def test_container_scoped_record_is_measurable(self, database, replicas):
        _, _, _, record = await _scenario(database, replicas=replicas, container=True)

        assert record.status is RecommendationStatus.VERIFIED
        assert record.verification_status == "passed"
        assert record.measured_cost_saved == pytest.approx(replicas * TRUE_COST_SAVING_PER_REPLICA, rel=0.05)
        assert record.measured_co2e_saved_grams == 0.0
        assert record.baseline is not None
        ledger_input = record.baseline.model_dump()["measured_ledger_input"]
        # Container rows carry no trusted cost yet: the change is measured on its workload.
        assert ledger_input["measurement_scope"] == "workload_fallback"
        assert ledger_input["co2e_outcome"] == "not_claimed"
