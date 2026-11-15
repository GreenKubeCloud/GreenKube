# tests/core/optimization/test_fixed_capacity_value.py
"""Fixed-capacity clusters: lanes and ranking from the change itself, not from a 0 realization (IMP-03)."""

from types import SimpleNamespace

import pytest

from greenkube.core.optimization.scoring import compute_value, resolve_parameters
from greenkube.models.metrics import EffortLevel, Recommendation, RecommendationRecord, RecommendationType, RiskLevel

GIB = 1024**3


def _fixed_nodes_impact(allocated: float, co2e: float = 0.0) -> dict:
    """An Impact Model projection on fixed nodes: realization 0, expected billable -0.0 / 0.0."""
    return {
        "kind": "container_resources",
        "currency": "USD",
        "allocated_cost_annual": allocated,
        "expected_billable_cost_annual": allocated * 0.0,
        "realization_factor": 0.0,
        "co2e_allocated_annual_g": co2e,
        "co2e_expected_annual_g": co2e * 0.0,
        "co2e_modeled": True,
        "confidence": 0.9,
        "assumptions": {"realization_factor_source": "realization_context"},
    }


def _params():
    return resolve_parameters(SimpleNamespace(CARBON_PRICE_PER_TONNE=100.0, RECOMMENDATION_INCIDENT_COST=100.0))


def _cpu_cut(current: int, proposed: int, allocated: float) -> Recommendation:
    return Recommendation(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        description="cut",
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=proposed,
        potential_savings_cost=0.0,
        risk_level=RiskLevel.MEDIUM,
        effort=EffortLevel.LOW,
        confidence=0.8,
        impact=_fixed_nodes_impact(allocated),
    )


def test_a_memory_increase_on_fixed_nodes_is_a_reliability_change():
    rec = Recommendation(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        description="raise",
        current_memory_request_bytes=GIB,
        recommended_memory_request_bytes=2 * GIB,
        potential_savings_cost=-0.0,
        risk_level=RiskLevel.MEDIUM,
        effort=EffortLevel.LOW,
        confidence=0.8,
        risk_factors=["oom_history"],
        impact=_fixed_nodes_impact(-120.0),
    )
    value = compute_value(rec, _params())
    assert value["lane"] == "reliability"
    assert value["incident_cost_avoided"] > 0


def test_a_large_cut_ranks_above_a_tiny_cut_on_fixed_nodes():
    large = compute_value(_cpu_cut(2000, 200, allocated=473.04), _params())
    tiny = compute_value(_cpu_cut(20, 10, allocated=2.63), _params())
    assert large["lane"] == tiny["lane"] == "savings"
    assert large["ev"] > tiny["ev"]
    assert large["value_annual"] > tiny["value_annual"]
    components = large["components"]
    assert components["allocated_cost_annual"] == pytest.approx(473.04)
    assert components["billable_cost_annual"] == pytest.approx(0.0)


def test_the_api_exposes_allocated_and_billable_projections_and_the_fixed_nodes_note():
    record = RecommendationRecord(
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cut",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=100,
        potential_savings_cost=0.0,
        impact=_fixed_nodes_impact(236.52, co2e=5000.0),
    )
    payload = record.model_dump(mode="json")
    assert payload["projected_allocated_cost_annual"] == pytest.approx(236.52)
    assert payload["projected_allocated_co2e_annual_g"] == pytest.approx(5000.0)
    assert payload["projected_billable_cost_annual"] == 0.0
    assert payload["projection_note"] == "fixed nodes: frees capacity, bill unchanged until nodes are removed"


def test_an_unobserved_cluster_reports_an_unknown_billable_saving():
    # IMP-12: before the autoscalers are observed (and without a stored context) the billable saving is unknown.
    impact = {
        **_fixed_nodes_impact(236.52),
        "expected_billable_cost_annual": 236.52,
        "realization_factor": 1.0,
        "assumptions": {"realization_factor_source": "realization_context:unobserved"},
    }
    record = RecommendationRecord(type=RecommendationType.RIGHTSIZING_CPU, description="cut", impact=impact)
    payload = record.model_dump(mode="json")
    assert payload["projected_allocated_cost_annual"] == pytest.approx(236.52)
    assert payload["projected_billable_cost_annual"] is None
    assert payload["projection_note"] == "billable saving unknown: cluster not observed yet"
