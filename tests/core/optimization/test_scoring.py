# tests/core/optimization/test_scoring.py
"""Tests for multi-criteria ranking."""

from greenkube.core.optimization.scoring import resolve_weights, score_recommendations
from greenkube.models.metrics import (
    EffortLevel,
    Recommendation,
    RecommendationType,
    RiskLevel,
)


def _rec(name, carbon=0.0, cost=0.0, risk=RiskLevel.LOW, effort=EffortLevel.LOW, confidence=0.8, patch=None):
    return Recommendation(
        pod_name=name,
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="pod",
        description=name,
        potential_savings_co2e_grams=carbon,
        potential_savings_cost=cost,
        risk_level=risk,
        effort=effort,
        confidence=confidence,
        patch=patch,
    )


class TestResolveWeights:
    def test_default_profile(self):
        assert resolve_weights(None) == resolve_weights("balanced")

    def test_unknown_profile_falls_back_to_balanced(self):
        assert resolve_weights("does-not-exist") == resolve_weights("balanced")

    def test_json_override(self):
        weights = resolve_weights("balanced", '{"carbon": 0.9}')
        assert weights["carbon"] == 0.9

    def test_invalid_json_is_ignored(self):
        assert resolve_weights("balanced", "{not json") == resolve_weights("balanced")


class TestScoreRecommendations:
    def test_factors_sum_to_score(self):
        scored = score_recommendations([_rec("a", carbon=100, cost=1)], profile="balanced")
        rec = scored[0]
        assert abs(sum(rec.ranking_factors.values()) - rec.ranking_score) < 0.01

    def test_carbon_first_ranks_carbon_heavy_first(self):
        low_carbon = _rec("low", carbon=1, cost=100)
        high_carbon = _rec("high", carbon=1000, cost=1)
        scored = score_recommendations([low_carbon, high_carbon], profile="carbon_first")
        ranked = sorted(scored, key=lambda r: r.ranking_score, reverse=True)
        assert ranked[0].pod_name == "high"

    def test_cost_first_ranks_cost_heavy_first(self):
        low_cost = _rec("low", carbon=100, cost=1)
        high_cost = _rec("high", carbon=1, cost=100)
        scored = score_recommendations([low_cost, high_cost], profile="cost_first")
        ranked = sorted(scored, key=lambda r: r.ranking_score, reverse=True)
        assert ranked[0].pod_name == "high"

    def test_low_risk_profile_penalizes_high_risk(self):
        risky = _rec("risky", carbon=100, cost=10, risk=RiskLevel.HIGH)
        safe = _rec("safe", carbon=100, cost=10, risk=RiskLevel.LOW)
        scored = score_recommendations([risky, safe], profile="low_risk")
        ranked = sorted(scored, key=lambda r: r.ranking_score, reverse=True)
        assert ranked[0].pod_name == "safe"

    def test_quick_wins_prefers_low_effort(self):
        heavy = _rec("heavy", carbon=100, cost=10, effort=EffortLevel.HIGH)
        easy = _rec("easy", carbon=100, cost=10, effort=EffortLevel.LOW)
        scored = score_recommendations([heavy, easy], profile="quick_wins")
        ranked = sorted(scored, key=lambda r: r.ranking_score, reverse=True)
        assert ranked[0].pod_name == "easy"

    def test_actionability_bonus_for_patch(self):
        with_patch = _rec("with", carbon=100, cost=10, patch={"kind": "Deployment"})
        without = _rec("without", carbon=100, cost=10)
        scored = score_recommendations([with_patch, without], profile="balanced")
        by_name = {r.pod_name: r for r in scored}
        assert by_name["with"].ranking_score > by_name["without"].ranking_score

    def test_empty_input(self):
        assert score_recommendations([], profile="balanced") == []
