"""Interim-method savings are reported in their own block, never in the measured totals (MEA-16).

The interim verifier (only with ``CHANGE_MEASUREMENT_ENABLED=false``) decides on
two window means with no counterfactual and no confounder check: its verified
amounts must not be summed with change-measured savings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.savings_report import assemble_savings_report, interim_recommendation_ids
from greenkube.models.measurement import INTERIM_METHOD
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.savings import SavingsAccrualTotal


def _verified(rec_id: int, measured_cost: float, *, interim: bool) -> RecommendationRecord:
    baseline = {"measurement_method": INTERIM_METHOD} if interim else {"change_id": f"change-{rec_id}"}
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
        baseline=baseline,
    )


def _measured(cost: float) -> SavingsAccrualTotal:
    return SavingsAccrualTotal(namespace="prod", method="measured", co2e_saved_grams=cost * 10, cost_saved_dollars=cost)


def test_interim_measurements_are_kept_out_of_the_measured_totals():
    records = [_verified(1, 1000.0, interim=False), _verified(2, 300.0, interim=True)]
    assert interim_recommendation_ids(records) == [2]
    report = assemble_savings_report(records, [_measured(50.0), _measured(20.0)], interim=[_measured(20.0)])

    assert report.run_rate_annual.cost == pytest.approx(1000.0)
    assert report.realized_to_date.cost == pytest.approx(50.0)
    assert report.interim_run_rate_annual is not None
    assert report.interim_run_rate_annual.cost == pytest.approx(300.0)
    assert report.interim_realized_to_date is not None
    assert report.interim_realized_to_date.cost == pytest.approx(20.0)
    assert report.verified_count == 1
    assert report.realization.measured_allocated_cost_annual == pytest.approx(1000.0)


async def test_the_interim_verifier_marks_the_records_it_decides():
    """A record decided by the interim verifier carries ``baseline.measurement_method``."""
    from unittest.mock import AsyncMock, MagicMock

    from greenkube.core.optimization.verifier import RecommendationVerifier
    from greenkube.models.measurement import MeasurementOutcome, SavingsMeasurement

    lifecycle = MagicMock()
    lifecycle.mark_verified = AsyncMock(side_effect=lambda rec_id, **kwargs: kwargs)
    verifier = RecommendationVerifier.__new__(RecommendationVerifier)
    verifier.lifecycle = lifecycle
    now = datetime.now(timezone.utc)
    stored = SavingsMeasurement(
        measurement_id="m-1",
        group_id="g-1",
        recommendation_id=1,
        outcome=MeasurementOutcome.REALIZED,
        windows={
            "method": INTERIM_METHOD,
            "before": {"start": (now - timedelta(days=6)).isoformat(), "end": (now - timedelta(days=3)).isoformat()},
            "after": {"start": (now - timedelta(days=3)).isoformat(), "end": now.isoformat()},
        },
        decided_at=now,
    )
    record = _verified(1, 0.0, interim=False)
    record.baseline = None
    captured = {}

    async def transition(rec_id, stored, baseline, details, cost, co2e):
        captured.update(baseline)
        return record

    verifier._transition = transition  # type: ignore[method-assign]
    await verifier.apply_stored_measurement(record, stored, {}, [], measured_cost=1.0, measured_co2e=1.0)
    assert captured["measurement_method"] == INTERIM_METHOD
