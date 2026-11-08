# tests/core/optimization/test_scoring.py
"""Risk-adjusted expected value ranking (roadmap I1/I2, BUG-23)."""

import random
from types import SimpleNamespace

import pytest

from greenkube.core.optimization.scoring import (
    PROFILES,
    compute_value,
    normalize_profile,
    ranking_sort_key,
    resolve_parameters,
    score_recommendations,
    sort_by_value,
    success_probability,
)
from greenkube.core.optimization.track_record import TrackRecord, TrackRecordBook
from greenkube.models.metrics import (
    EffortLevel,
    Recommendation,
    RecommendationRecord,
    RecommendationType,
    RiskLevel,
)


def _rec(
    name,
    carbon=0.0,
    cost=0.0,
    risk=RiskLevel.LOW,
    effort=EffortLevel.LOW,
    confidence=0.8,
    namespace="prod",
    rec_type=RecommendationType.RIGHTSIZING_CPU,
    scope="pod",
    impact=None,
    risk_factors=None,
):
    return Recommendation(
        pod_name=name,
        namespace=namespace,
        type=rec_type,
        scope=scope,
        description=name,
        potential_savings_co2e_grams=carbon,
        potential_savings_cost=cost,
        risk_level=risk,
        effort=effort,
        confidence=confidence,
        impact=impact,
        risk_factors=risk_factors or [],
    )


def _config(**overrides):
    values = {
        "CARBON_PRICE_PER_TONNE": 100.0,
        "RECOMMENDATION_RANKING_PROFILE": "balanced",
        "RECOMMENDATION_INCIDENT_COST": 100.0,
        "RECOMMENDATION_ENGINEER_HOUR_COST": 0.0,
        "IMPACT_PRICING_CURRENCY": "EUR",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestParameters:
    def test_profiles_are_presets_over_the_ev_parameters(self):
        config = _config(CARBON_PRICE_PER_TONNE=80.0, RECOMMENDATION_ENGINEER_HOUR_COST=50.0)
        assert resolve_parameters(config, "balanced").carbon_price_per_tonne == 80.0
        assert resolve_parameters(config, "cost_first").carbon_price_per_tonne == 0.0
        assert resolve_parameters(config, "carbon_first").carbon_price_per_tonne == 320.0
        assert resolve_parameters(config, "low_risk").risk_aversion > resolve_parameters(config).risk_aversion
        quick = resolve_parameters(config, "quick_wins")
        assert quick.effort_weight > 1.0 and quick.engineer_hour_cost == 100.0
        assert set(PROFILES) == {"balanced", "cost_first", "carbon_first", "low_risk", "quick_wins"}

    def test_carbon_price_override_replaces_the_profile_price(self):
        params = resolve_parameters(_config(), "carbon_first", carbon_price=10.0)
        assert params.carbon_price_per_tonne == 10.0

    def test_configured_profile_is_the_default(self):
        assert resolve_parameters(_config(RECOMMENDATION_RANKING_PROFILE="low_risk")).profile == "low_risk"

    def test_unknown_requested_profile_is_rejected(self):
        with pytest.raises(ValueError):
            normalize_profile("does-not-exist")
        with pytest.raises(ValueError):
            resolve_parameters(_config(), "does-not-exist")

    def test_unknown_configured_profile_falls_back_to_balanced(self):
        assert resolve_parameters(_config(RECOMMENDATION_RANKING_PROFILE="nope")).profile == "balanced"

    def test_deprecated_weights_are_ignored_with_a_warning(self, caplog, monkeypatch):
        monkeypatch.setattr("greenkube.core.optimization.scoring._weights_warning_logged", False)
        with caplog.at_level("WARNING"):
            params = resolve_parameters(_config(RECOMMENDATION_RANKING_WEIGHTS='{"carbon": 0.9}'))
            resolve_parameters(_config(RECOMMENDATION_RANKING_WEIGHTS='{"carbon": 0.9}'))
        assert params == resolve_parameters(_config())
        assert sum("RECOMMENDATION_RANKING_WEIGHTS is deprecated" in r.message for r in caplog.records) == 1

    def test_negative_carbon_price_is_rejected(self):
        with pytest.raises(ValueError):
            resolve_parameters(_config(), carbon_price=-1.0)


class TestValueScore:
    def test_value_annual_uses_the_impact_projection_and_the_carbon_price(self):
        impact = {
            "expected_billable_cost_annual": 120.0,
            "co2e_expected_annual_g": 500_000.0,
            "co2e_modeled": True,
            "currency": "EUR",
        }
        value = compute_value(_rec("a", cost=999.0, carbon=1.0, impact=impact), resolve_parameters(_config()))
        assert value["value_annual"] == pytest.approx(120.0 + 100.0 * 0.5)
        assert value["currency"] == "EUR"
        assert value["components"]["cost_annual"] == 120.0
        assert value["method"] == "projected"

    def test_unmodeled_carbon_is_not_valued(self):
        impact = {"expected_billable_cost_annual": 50.0, "co2e_expected_annual_g": 9e9, "co2e_modeled": False}
        value = compute_value(_rec("pv", impact=impact), resolve_parameters(_config()))
        assert value["value_annual"] == pytest.approx(50.0)

    def test_legacy_rows_use_the_projected_columns(self):
        value = compute_value(_rec("a", cost=10.0, carbon=2_000_000.0), resolve_parameters(_config()))
        assert value["value_annual"] == pytest.approx(10.0 + 200.0)

    def test_ev_formula(self):
        rec = _rec("a", cost=100.0, risk=RiskLevel.MEDIUM, confidence=0.7)
        params = resolve_parameters(_config(RECOMMENDATION_ENGINEER_HOUR_COST=40.0))
        value = compute_value(rec, params, delivery_mode="manual")
        p = value["p_success"]
        incident = 100.0 * 0.25  # medium severity, pod scope, normal criticality
        effort = 0.5 * 40.0
        assert value["incident_cost"] == pytest.approx(incident)
        assert value["effort_cost"] == pytest.approx(effort)
        assert value["ev"] == pytest.approx(p * 100.0 - (1 - p) * incident - effort, abs=0.01)

    def test_factors_are_in_currency_and_sum_to_the_score(self):
        (rec,) = score_recommendations(
            [_rec("a", cost=80.0, carbon=300_000.0, risk=RiskLevel.HIGH)],
            config=_config(RECOMMENDATION_ENGINEER_HOUR_COST=30.0),
        )
        assert set(rec.ranking_factors) == {"cost", "carbon", "incident_risk", "effort"}
        assert rec.ranking_factors["incident_risk"] < 0
        assert sum(rec.ranking_factors.values()) == pytest.approx(rec.ranking_score, abs=0.05)
        assert rec.ranking_score == rec.value["ev"]

    def test_blast_radius_grows_with_scope_criticality_and_singletons(self):
        params = resolve_parameters(_config())
        pod = compute_value(_rec("a", cost=10.0, risk=RiskLevel.HIGH), params)
        node = compute_value(_rec("n", cost=10.0, risk=RiskLevel.HIGH, scope="node"), params)
        critical = compute_value(_rec("c", cost=10.0, risk=RiskLevel.HIGH), params, criticality="critical")
        single = compute_value(_rec("s", cost=10.0, risk=RiskLevel.HIGH, risk_factors=["singleton_workload"]), params)
        assert node["incident_cost"] > pod["incident_cost"]
        assert critical["incident_cost"] == pytest.approx(4 * pod["incident_cost"])
        assert single["incident_cost"] == pytest.approx(1.5 * pod["incident_cost"])

    def test_pull_request_delivery_halves_the_effort(self):
        params = resolve_parameters(_config(RECOMMENDATION_ENGINEER_HOUR_COST=100.0))
        rec = _rec("a", cost=10.0, effort=EffortLevel.MEDIUM)
        direct = compute_value(rec, params, delivery_mode="direct")
        manual = compute_value(rec, params, delivery_mode="manual")
        assert direct["effort_cost"] == pytest.approx(manual["effort_cost"] / 2)

    def test_reliability_recommendations_count_the_incident_they_avoid(self):
        params = resolve_parameters(_config())
        increase = _rec("up", cost=-20.0, risk_factors=["oom_history"])
        value = compute_value(increase, params)
        assert value["lane"] == "reliability"
        assert value["incident_cost_avoided"] == pytest.approx(0.5 * 100.0)
        assert value["ev"] > value["p_success"] * value["value_annual"]


class TestSuccessProbability:
    def test_confidence_is_the_prior(self):
        p, _, _ = success_probability(0.8, "low")
        assert p == pytest.approx(0.8)

    def test_risk_class_lowers_success(self):
        assert success_probability(0.8, "high")[0] < success_probability(0.8, "low")[0]

    def test_track_record_moves_the_posterior(self):
        good = TrackRecord("vpa", "cpu_rightsizing", realized=14, not_realized=1, rollback=1)
        bad = TrackRecord("vpa", "cpu_rightsizing", realized=1, not_realized=10)
        prior = success_probability(0.6, "low")[0]
        assert success_probability(0.6, "low", good)[0] > prior > success_probability(0.6, "low", bad)[0]
        assert success_probability(0.6, "low", good)[0] == pytest.approx((0.6 * 4 + 14) / (4 + 16))

    def test_track_record_is_looked_up_per_source_and_capability(self):
        book = TrackRecordBook(
            records={("vpa", "cpu_rightsizing"): TrackRecord("vpa", "cpu_rightsizing", realized=0, not_realized=20)}
        )
        vpa = _rec("v", cost=50.0).model_copy(update={"source": "vpa"})
        native = _rec("g", cost=50.0)
        scored = {r.pod_name: r for r in score_recommendations([vpa, native], config=_config(), track_record=book)}
        assert scored["v"].value["p_success"] < scored["g"].value["p_success"]
        assert scored["v"].value["components"]["track_record"]["failed"] == 20


class TestRanking:
    def test_carbon_first_ranks_carbon_heavy_first(self):
        low_carbon = _rec("low", carbon=1, cost=100)
        high_carbon = _rec("high", carbon=5_000_000, cost=1)
        ranked = sort_by_value(score_recommendations([low_carbon, high_carbon], "carbon_first", config=_config()))
        assert ranked[0].pod_name == "high"

    def test_cost_first_ranks_cost_heavy_first(self):
        low_cost = _rec("low", carbon=900_000, cost=1)
        high_cost = _rec("high", carbon=1, cost=100)
        ranked = sort_by_value(score_recommendations([low_cost, high_cost], "cost_first", config=_config()))
        assert ranked[0].pod_name == "high"

    def test_low_risk_profile_penalizes_high_risk(self):
        risky = _rec("risky", cost=40, risk=RiskLevel.HIGH)
        safe = _rec("safe", cost=30, risk=RiskLevel.LOW)
        ranked = sort_by_value(score_recommendations([risky, safe], "low_risk", config=_config()))
        assert ranked[0].pod_name == "safe"

    def test_quick_wins_prefers_low_effort(self):
        heavy = _rec("heavy", cost=100, effort=EffortLevel.HIGH)
        easy = _rec("easy", cost=90, effort=EffortLevel.LOW)
        ranked = sort_by_value(score_recommendations([heavy, easy], "quick_wins", config=_config()))
        assert ranked[0].pod_name == "easy"

    def test_records_keep_their_type(self):
        record = RecommendationRecord.from_recommendation(_rec("a", cost=10.0))
        (scored,) = score_recommendations([record], config=_config())
        assert isinstance(scored, RecommendationRecord)
        assert scored.value is not None
        assert scored.value["ev"] == scored.ranking_score

    def test_empty_input(self):
        assert score_recommendations([], profile="balanced") == []

    def test_score_does_not_depend_on_the_batch(self):
        """BUG-23: batch min-max normalization changed a score when the set changed."""
        a = _rec("a", carbon=100_000, cost=10)
        b = _rec("b", carbon=900_000, cost=200)
        c = _rec("c", carbon=1, cost=1)
        alone = score_recommendations([a], config=_config())[0].ranking_score
        together = {r.pod_name: r.ranking_score for r in score_recommendations([a, b, c], config=_config())}
        assert together["a"] == alone
        assert alone > 0

    def test_ranking_is_stable_across_namespace_filters(self):
        """Property: filtering by namespace keeps the relative order of what remains (I definition of done)."""
        rng = random.Random(20261108)
        namespaces = ["prod", "staging", "batch", "web"]
        for trial in range(200):
            recs = []
            for index in range(rng.randint(1, 25)):
                recs.append(
                    _rec(
                        f"pod-{trial}-{index}",
                        carbon=rng.choice([0.0, rng.uniform(0, 5e6)]),
                        cost=rng.choice([0.0, rng.uniform(-50, 500)]),
                        risk=rng.choice(list(RiskLevel)),
                        effort=rng.choice(list(EffortLevel)),
                        confidence=rng.uniform(0, 1),
                        namespace=rng.choice(namespaces),
                        scope=rng.choice(["pod", "workload", "namespace", "node"]),
                    ).model_copy(update={"id": index + 1})
                )
            profile = rng.choice(list(PROFILES))
            full = [r.pod_name for r in sort_by_value(score_recommendations(recs, profile, config=_config()))]
            for namespace in namespaces:
                subset = [r for r in recs if r.namespace == namespace]
                filtered = [r.pod_name for r in sort_by_value(score_recommendations(subset, profile, config=_config()))]
                assert filtered == [name for name in full if name in set(filtered)]

    def test_sort_key_puts_unscored_rows_last(self):
        scored = score_recommendations([_rec("a", cost=1.0)], config=_config())[0]
        unscored = RecommendationRecord.from_recommendation(_rec("b", cost=100.0))
        assert sorted([unscored, scored], key=ranking_sort_key)[0].pod_name == "a"
