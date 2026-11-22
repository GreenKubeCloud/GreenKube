"""Component split never books more than the change's net saving (production readiness, C5).

Before the fix a bundle that cut CPU (-$100/yr) and raised memory (+$80/yr)
booked the gross CPU saving ($100/yr) on the CPU recommendation while the
memory increase was dropped: the change claimed $100 for a net $20.
"""

from __future__ import annotations

import pytest

from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.verify.attribution import allocate, allocated_projection, component_cap
from greenkube.verify.model import ChangeOutcome, SavingsEstimate

HOURS = 8760.0


def _record(rec_id: int, rec_type: RecommendationType, **values) -> RecommendationRecord:
    return RecommendationRecord(
        id=rec_id,
        type=rec_type,
        description="x",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        status=RecommendationStatus.VERIFYING,
        **values,
    )


def _estimate(quantity: str, annual: float) -> SavingsEstimate:
    return SavingsEstimate(
        quantity=quantity,
        unit="currency",
        claimed=quantity == "allocated_cost",
        rate_per_hour=annual / HOURS,
        aligned_hours=150,
        annualized=annual,
        outcome=ChangeOutcome.REALIZED if annual > 0 else ChangeOutcome.NOT_REALIZED,
    )


RECORDS = [
    _record(
        1,
        RecommendationType.RIGHTSIZING_CPU,
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
    ),
    _record(
        2,
        RecommendationType.RIGHTSIZING_MEMORY,
        current_memory_request_bytes=1024**3,
        recommended_memory_request_bytes=2 * 1024**3,
    ),
]


def test_a_cpu_cut_and_memory_increase_bundle_books_the_net_saving() -> None:
    estimates = [_estimate("allocated_cost", 20.0), _estimate("cost_cpu", 100.0), _estimate("cost_ram", -80.0)]
    shares = {share.record.id: share for share in allocate(RECORDS, estimates)}

    assert shares[1].quantity == "cost_cpu" and shares[1].share == pytest.approx(0.2)
    # The memory increase keeps its signed estimate (not realized, never claimed).
    assert shares[2].quantity == "cost_ram" and shares[2].share == pytest.approx(1.0)
    claimed = 100.0 * shares[1].share
    assert claimed == pytest.approx(20.0)


def test_components_that_add_up_to_the_net_saving_are_not_scaled() -> None:
    estimates = [_estimate("allocated_cost", 130.0), _estimate("cost_cpu", 100.0), _estimate("cost_ram", 30.0)]
    assert component_cap(estimates, ("cost_cpu", "cost_ram")) == 1.0
    shares = {share.record.id: share.share for share in allocate(RECORDS, estimates)}
    assert shares == {1: pytest.approx(1.0), 2: pytest.approx(1.0)}


def test_a_component_saving_without_a_net_saving_is_not_claimed() -> None:
    estimates = [_estimate("allocated_cost", -5.0), _estimate("cost_cpu", 100.0), _estimate("cost_ram", -105.0)]
    assert component_cap(estimates, ("cost_cpu", "cost_ram")) == 0.0


def test_projected_shares_weigh_the_allocated_projection_not_the_billable_one() -> None:
    """With no autoscaler (realization 0) the billable projection is 0; shares follow the allocated one."""
    records = [
        _record(
            1, RecommendationType.RIGHTSIZING_CPU, potential_savings_cost=0.0, impact={"allocated_cost_annual": 300.0}
        ),
        _record(
            2,
            RecommendationType.RIGHTSIZING_MEMORY,
            potential_savings_cost=0.0,
            impact={"allocated_cost_annual": 100.0},
        ),
    ]
    assert allocated_projection(records[0]) == 300.0
    shares = allocate(records, [_estimate("allocated_cost", 400.0)])
    assert [share.share for share in shares] == [pytest.approx(0.75), pytest.approx(0.25)]
