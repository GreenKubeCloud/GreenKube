# tests/core/optimization/test_verifier_co2e_alignment.py
"""The interim verifier measures CO2e like change measurement (Impact Model, component matching).

Before: the interim verifier compared a measured CO2e delta (operational CO2e
of stored rows) with ``potential_savings_co2e_grams`` (a total that includes
embodied emissions the rows never carry), split a group's CO2e by projected
*cost*, and never claimed the CO2e of a rightsizing even when the rows were
allocated by the Impact Model (idle power by reservation), whose CO2e moves
with requests. Change measurement already did all three the matching way.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.verifier import RecommendationVerifier, window_rates
from greenkube.impact.comparison import measurable_co2e_projection
from greenkube.models.measurement import QuantityOutcome
from greenkube.models.metrics import CombinedMetric, RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

HOURS_PER_YEAR = 8766.0


def _impact(idle: float, embodied: float) -> dict:
    return {
        "model_version": "2.0.0",
        "co2e_modeled": True,
        "realization_factor": 1.0,
        "co2e_breakdown_annual_g": {
            "idle_operational": idle,
            "dynamic_operational": 0.0,
            "memory_operational": 0.0,
            "gpu_operational": 0.0,
            "embodied": embodied,
            "total": idle + embodied,
        },
    }


@pytest.fixture
async def database():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield db_manager
    await db_manager.close()


def _rows(now: datetime, applied_at: datetime, *, model_version: Optional[str]) -> list[CombinedMetric]:
    rows: list[CombinedMetric] = []
    moment = (applied_at - timedelta(hours=80)).replace(second=0, microsecond=0)
    moment = moment.replace(minute=(moment.minute // 5) * 5)
    while moment < now:
        after = moment >= applied_at
        rows.append(
            CombinedMetric(
                pod_name=f"api-{'new' if after else 'old'}",
                namespace="prod",
                owner_kind="Deployment",
                owner_name="api",
                total_cost=0.008 if after else 0.01,
                # Reservation-aware operational CO2e: the idle share drops with the request.
                co2e_grams=0.6 if after else 1.0,
                embodied_co2e_grams=0.5 if after else 0.8,
                cpu_request=300 if after else 1000,
                cpu_usage_millicores=200,
                restart_count=0,
                timestamp=moment,
                duration_seconds=300,
                is_estimated=False,
                model_version=model_version,
            )
        )
        moment += timedelta(minutes=5)
    return rows


async def _verify(database, *, model_version: Optional[str]) -> RecommendationRecord:
    cfg = Config()
    now = datetime.now(timezone.utc)
    applied_at = now - timedelta(hours=80)
    combined = SQLiteCombinedMetricsRepository(database)
    await combined.write_combined_metrics(_rows(now, applied_at, model_version=model_version))
    await MetricsCompressor(database, cfg).run()
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
                status=RecommendationStatus.APPLIED,
                current_cpu_request_millicores=1000,
                recommended_cpu_request_millicores=300,
                potential_savings_cost=1000.0,
                potential_savings_co2e_grams=90_000.0,
                impact=_impact(idle=40_000.0, embodied=50_000.0),
                applied_at=applied_at,
                application_method="detected",
                baseline={"restart_count": 0, "proposed": {"cpu_request_millicores": 300}},
                verification_status="pending",
            )
        ]
    )
    verifier = RecommendationVerifier(RecommendationLifecycle(reco), combined, config=cfg)
    await verifier.verify_due()
    return (await reco.get_recommendations_by_statuses([status.value for status in RecommendationStatus]))[0]


def _co2e(record: RecommendationRecord) -> dict:
    assert record.baseline is not None
    measurement = record.baseline.model_dump()
    ledger = measurement["measured_ledger_input"]
    return ledger


async def test_impact_model_rows_claim_the_rightsizing_co2e_against_the_operational_projection(database):
    record = await _verify(database, model_version="2.0.0")
    assert record.status is RecommendationStatus.VERIFIED
    # 0.4 g per 5 minutes -> 4.8 g/h of operational CO2e freed by the request cut.
    assert record.measured_co2e_saved_grams == pytest.approx(4.8 * HOURS_PER_YEAR, rel=0.05)
    assert _co2e(record)["co2e_outcome"] == "realized"


async def test_rows_without_impact_allocation_keep_the_co2e_unclaimed(database):
    record = await _verify(database, model_version=None)
    assert record.status is RecommendationStatus.VERIFIED
    assert record.measured_co2e_saved_grams == 0.0
    assert _co2e(record)["co2e_outcome"] == "not_claimed"


async def test_co2e_projection_is_component_matched(database):
    from greenkube.storage.measurement_repository import SavingsMeasurementRepository

    record = await _verify(database, model_version="2.0.0")
    assert record.id is not None
    rows = await SavingsMeasurementRepository(database).list_for_recommendation(record.id)
    co2e = next(item for item in rows[-1].estimates if item.quantity == "co2e_g")
    assert co2e.outcome is QuantityOutcome.REALIZED
    # The projection it is compared with excludes the 50 kg of embodied CO2e the rows do not carry.
    assert co2e.projected_annual == pytest.approx(40_000.0)
    assert co2e.projected_annual == pytest.approx(measurable_co2e_projection(_impact(40_000.0, 50_000.0)))
    assert co2e.projection_accuracy == pytest.approx(4.8 * HOURS_PER_YEAR / 40_000.0, rel=0.05)


def test_window_rates_flag_impact_model_allocation_only_when_every_carbon_row_has_it():
    now = datetime.now(timezone.utc)
    rows = _rows(now, now - timedelta(hours=1), model_version="2.0.0")
    assert window_rates(rows, 300).co2e_model_allocated is True
    rows[0] = rows[0].model_copy(update={"model_version": None})
    assert window_rates(rows, 300).co2e_model_allocated is False
