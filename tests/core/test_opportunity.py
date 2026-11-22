"""The projected opportunity is computed once, server side (IMP-07).

Request changes and asset deletions add up; request increases are a cost
reported apart, never netted into the opportunity.
"""

from __future__ import annotations

import pytest

from greenkube.core.opportunity import OPPORTUNITY_LABEL, opportunity_of
from greenkube.core.savings_report import assemble_savings_report
from greenkube.models.metrics import RecommendationRecord, RecommendationType


def _rec(rec_id, kind, namespace, value, *, owner=None, current=None, recommended=None) -> RecommendationRecord:
    fields: dict[str, object] = {}
    if kind is RecommendationType.RIGHTSIZING_MEMORY:
        fields = {"current_memory_request_bytes": current, "recommended_memory_request_bytes": recommended}
    elif kind is RecommendationType.RIGHTSIZING_CPU:
        fields = {"current_cpu_request_millicores": current, "recommended_cpu_request_millicores": recommended}
    record = RecommendationRecord(
        id=rec_id,
        pod_name=owner or namespace,
        namespace=namespace,
        owner_kind="Deployment" if owner else None,
        owner_name=owner,
        type=kind,
        description="x",
        potential_savings_cost=value,
        potential_savings_co2e_grams=value * 10,
    )
    return record.model_copy(update=fields)


def _fixture() -> list[RecommendationRecord]:
    return [
        _rec(1, RecommendationType.ORPHANED_LOAD_BALANCER, "ns1", 100.0),
        _rec(2, RecommendationType.RIGHTSIZING_CPU, "ns1", 20.0, owner="a", current=1000, recommended=500),
        _rec(3, RecommendationType.RIGHTSIZING_CPU, "ns1", 30.0, owner="b", current=1000, recommended=500),
        _rec(4, RecommendationType.RIGHTSIZING_MEMORY, "ns2", -5.0, owner="c", current=1, recommended=2),
        _rec(5, RecommendationType.RIGHTSIZING_CPU, "ns2", 10.0, owner="d", current=1000, recommended=500),
    ]


def test_savings_add_up_and_increases_are_reported_apart():
    summary = opportunity_of(_fixture())
    assert summary.cost_annual == pytest.approx(160.0)
    assert summary.co2e_grams_annual == pytest.approx(1600.0)
    assert summary.reliability_cost_annual == pytest.approx(5.0)
    assert summary.reliability_count == 1
    assert summary.recommendation_count == 4
    assert summary.label == OPPORTUNITY_LABEL


def test_the_reliability_cost_is_the_allocated_cost_on_fixed_nodes():
    """Fixed nodes: the billable projection of an increase is 0, its allocated cost is not."""
    increase = _rec(1, RecommendationType.RIGHTSIZING_MEMORY, "ns", 0.0, owner="a", current=1, recommended=2)
    increase = increase.model_copy(update={"impact": {"allocated_cost_annual": -12.0, "realization_factor": 0.0}})
    summary = opportunity_of([increase])
    assert summary.reliability_count == 1
    assert summary.reliability_cost_annual == pytest.approx(12.0)


def test_the_savings_report_serves_the_opportunity_to_every_page():
    report = assemble_savings_report([], [], active=_fixture())
    assert report.opportunity is not None
    assert report.opportunity.cost_annual == pytest.approx(160.0)
    assert report.opportunity.reliability_cost_annual == pytest.approx(5.0)
