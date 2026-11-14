"""API fields that tell measured allocated savings from realized billed ones (production readiness, B6/C2/C7).

GreenKube measures allocated savings; the bill drops only by the realization
factor. The v1 savings report and the v2 change measurements say so
explicitly, next to the existing (unchanged) fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.savings_report import assemble_savings_report, realization_summary
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from tests.api.test_changes_api import _client, measured  # noqa: F401  # pyrefly: ignore[missing-import]


def _verified(rec_id: int, measured_cost: float, factor) -> RecommendationRecord:
    impact = {"allocated_cost_annual": measured_cost}
    if factor is not None:
        impact["realization_factor"] = factor
    return RecommendationRecord(
        id=rec_id,
        pod_name=f"pod-{rec_id}",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cut cpu",
        status=RecommendationStatus.VERIFIED,
        verification_status="passed",
        verified_at=datetime.now(timezone.utc) - timedelta(days=1),
        measured_cost_saved=measured_cost,
        measured_co2e_saved_grams=measured_cost * 10,
        impact=impact,
    )


def test_the_savings_report_separates_measured_allocated_from_expected_billed() -> None:
    records = [_verified(1, 1000.0, 0.25), _verified(2, 500.0, 1.0)]
    report = assemble_savings_report(records, [])

    assert report.measured_basis == "measured_allocated"
    # Existing fields keep their meaning and values.
    assert report.run_rate_annual.cost == pytest.approx(1500.0)
    realization = report.realization
    assert realization.measured_allocated_cost_annual == pytest.approx(1500.0)
    assert realization.realized_billed_cost_annual_expected == pytest.approx(1000.0 * 0.25 + 500.0)
    assert realization.realization_factor == pytest.approx(750.0 / 1500.0)
    assert realization.realized_billed_measured is False
    assert realization.recommendation_count == 2 and realization.without_realization_count == 0


def test_a_projection_without_a_realization_factor_is_counted_and_left_out() -> None:
    summary = realization_summary([_verified(1, 1000.0, None), _verified(2, 400.0, 0.5)])
    assert summary.measured_allocated_cost_annual == pytest.approx(1400.0)
    assert summary.realized_billed_cost_annual_expected == pytest.approx(200.0)
    assert summary.realization_factor == pytest.approx(0.5)
    assert summary.without_realization_count == 1
    assert realization_summary([]).realization_factor is None


def test_a_measurement_records_the_allocated_weighted_realization_factor() -> None:
    from greenkube.verify.service import realization_factor

    records = [_verified(1, 300.0, 0.25), _verified(2, 100.0, 1.0)]
    assert realization_factor(records) == pytest.approx((300.0 * 0.25 + 100.0) / 400.0)
    assert realization_factor([_verified(1, 300.0, None)]) is None


async def test_change_measurements_state_their_basis(measured):  # noqa: F811
    scenario, change, _ = measured
    with _client(scenario) as client:
        payload = client.get(f"/api/v2/changes/{change.change_id}/measurements").json()
    basis = payload["items"][0]["savings_basis"]
    assert basis["measured_method"] == "measured_allocated"
    assert basis["projection_basis"] == "allocated"
    assert basis["measured_allocated_annual"] is not None and basis["measured_allocated_annual"] > 0
    assert basis["realized_billed_measured"] is False
    # The scenario's recommendations carry no Impact Model projection: no factor, no billed expectation.
    assert basis["realization_factor"] is None and basis["realized_billed_annual_expected"] is None
