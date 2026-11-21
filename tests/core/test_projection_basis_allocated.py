"""Pending projections and user declarations are on the allocated basis, like measurements (IMP-06).

On a fixed-node cluster the billable projection of a rightsizing is 0 (realization
factor 0): the pending 'projected' panel must still show the allocated saving the
measurement will be compared with, and a declared saving must be bounded by it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.savings_attributor import projected_allocated_rates, validate_user_declared_savings
from greenkube.core.savings_report import assemble_savings_report
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType


def _applied(actual_cpu: int = 100) -> RecommendationRecord:
    return RecommendationRecord(
        id=1,
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cut cpu",
        status=RecommendationStatus.APPLIED,
        verification_status="pending",
        applied_at=datetime.now(timezone.utc) - timedelta(days=1),
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=100,
        actual_cpu_request_millicores=actual_cpu,
        potential_savings_cost=0.0,
        potential_savings_co2e_grams=0.0,
        cost_saved=0.0,
        carbon_saved_co2e_grams=0.0,
        impact={"allocated_cost_annual": 236.52, "realization_factor": 0.0, "expected_billable_cost_annual": 0.0},
    )


def test_a_fixed_node_cut_applied_in_full_projects_its_allocated_saving():
    report = assemble_savings_report([_applied()], [])
    assert report.projected_annual.cost == pytest.approx(236.52)
    assert report.projected_annual.value_basis == "allocated"
    assert report.realization.projected_billable_cost_annual_expected == pytest.approx(0.0)


def test_a_partial_apply_scales_the_allocated_projection():
    """1000m -> 100m recommended, 550m applied: half of the projected cut."""
    rates = projected_allocated_rates(_applied(actual_cpu=550))
    assert rates is not None
    assert rates[0] == pytest.approx(236.52 / 2)


def test_a_declared_saving_is_bounded_by_the_allocated_projection():
    record = _applied()
    declared = validate_user_declared_savings(record, co2e_grams_per_year=None, cost_per_year=200.0)
    assert declared is not None and declared.cost_per_year == 200.0
    assert declared.projected_cost_per_year == pytest.approx(236.52)
    with pytest.raises(ValueError):
        validate_user_declared_savings(record, co2e_grams_per_year=None, cost_per_year=3000.0)
