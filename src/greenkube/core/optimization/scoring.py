# src/greenkube/core/optimization/scoring.py
"""Risk-adjusted expected value of recommendations (ValueScore, roadmap I1/I2).

Every recommendation gets an absolute score in currency per year::

    value_annual  = expected_billable_cost_annual + carbon_price x tCO2e_expected_annual
    p_success     = Beta posterior mean: confidence prior updated by the source track record,
                    times a risk-class factor
    incident_cost = risk_aversion x incident_cost[risk_level] x blast_radius
    effort_cost   = effort_hours[effort, delivery_mode] x engineer_hour_cost x effort_weight
    EV            = p_success x value_annual - (1 - p_success) x incident_cost - effort_cost

Reliability recommendations (``value_annual < 0``: the change costs money) also
count the incident cost they avoid. Scores are absolute, so the order of two
recommendations never depends on which other recommendations are ranked with
them (the batch min-max normalization of BUG-23 is gone).

Profiles are presets over the carbon price, the risk aversion and the effort
weight; ``ranking_factors`` break the EV down in currency and sum to it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from greenkube.core.optimization.risks import normalize_criticality
from greenkube.core.optimization.track_record import EMPTY_BOOK, TrackRecordBook

logger = logging.getLogger(__name__)

#: Version of the EV formula, persisted with every ValueScore.
VALUE_MODEL = "ev/1"

DEFAULT_PROFILE = "balanced"


@dataclass(frozen=True)
class RankingProfile:
    """A preset over the EV parameters (roadmap I2)."""

    name: str
    #: Multiplier on the configured carbon price (0 ignores carbon).
    carbon_price_multiplier: float = 1.0
    risk_aversion: float = 1.0
    effort_weight: float = 1.0
    #: Engineer hour cost used when the configured one is lower (quick wins weigh effort even at 0).
    engineer_hour_cost_floor: float = 0.0
    description: str = ""


PROFILES: Dict[str, RankingProfile] = {
    "balanced": RankingProfile("balanced", description="Configured carbon price, neutral risk aversion."),
    "cost_first": RankingProfile(
        "cost_first", carbon_price_multiplier=0.0, description="Cost only: carbon priced at 0."
    ),
    "carbon_first": RankingProfile(
        "carbon_first", carbon_price_multiplier=4.0, description="Carbon priced at 4x the configured price."
    ),
    "low_risk": RankingProfile("low_risk", risk_aversion=4.0, description="Incident costs weighed 4x."),
    "quick_wins": RankingProfile(
        "quick_wins",
        effort_weight=3.0,
        engineer_hour_cost_floor=100.0,
        description="Effort weighed 3x, at 100 per engineer hour at least.",
    ),
}

#: Probability multiplier of success by risk class.
RISK_SUCCESS_FACTOR = {"low": 1.0, "medium": 0.95, "high": 0.85}
#: Share of the configured incident cost a failed change of each risk class is expected to cost.
INCIDENT_SEVERITY = {"low": 0.05, "medium": 0.25, "high": 1.0}
#: Blast-radius multipliers.
CRITICALITY_BLAST = {"critical": 4.0, "high": 2.0, "normal": 1.0, "low": 0.5}
SCOPE_BLAST = {"node": 3.0, "cluster": 3.0, "namespace": 2.0}
SINGLETON_BLAST = 1.5
#: Engineer hours by effort level; a pull request opened by GreenKube halves them.
EFFORT_HOURS = {"low": 0.5, "medium": 2.0, "high": 8.0}
DIRECT_DELIVERY_EFFORT = 0.5
#: Pseudo-observations carried by the analyzer's confidence in the Beta prior.
CONFIDENCE_PRIOR_STRENGTH = 4.0
#: Probability that a workload with OOM/restart/throttling history has an incident without the change.
INCIDENT_PROBABILITY_WITH_HISTORY = 0.5
INCIDENT_PROBABILITY_BASELINE = 0.1
_HISTORY_FACTORS = ("restart_history", "oom_history", "cpu_throttling_history")


@dataclass(frozen=True)
class ValueParameters:
    """Resolved EV parameters for one ranking request."""

    profile: str
    carbon_price_per_tonne: float
    risk_aversion: float
    effort_weight: float
    engineer_hour_cost: float
    incident_cost: float
    currency: str
    #: Weight of the released allocated value (allocated - expected billable) in ``value_annual`` (IMP-03).
    capacity_value_weight: float = 1.0


def profile_names() -> List[str]:
    return list(PROFILES)


def normalize_profile(profile: Optional[str]) -> str:
    """Canonical profile name; raises ValueError for an unknown one."""
    name = (profile or DEFAULT_PROFILE).strip().lower()
    if name not in PROFILES:
        raise ValueError(f"Unknown ranking profile {profile!r}. Use one of: {', '.join(PROFILES)}.")
    return name


def _config_float(config: Any, name: str, default: float) -> float:
    value = getattr(config, name, default) if config is not None else default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


_weights_warning_logged = False


def _warn_deprecated_weights(config: Any) -> None:
    """RECOMMENDATION_RANKING_WEIGHTS tuned the retired batch-normalized score: say once that it is ignored."""
    global _weights_warning_logged
    if _weights_warning_logged or not str(getattr(config, "RECOMMENDATION_RANKING_WEIGHTS", "") or "").strip():
        return
    _weights_warning_logged = True
    logger.warning(
        "RECOMMENDATION_RANKING_WEIGHTS is deprecated and ignored: recommendations are ranked by expected value. "
        "Use RECOMMENDATION_RANKING_PROFILE, CARBON_PRICE_PER_TONNE, RECOMMENDATION_INCIDENT_COST and "
        "RECOMMENDATION_ENGINEER_HOUR_COST instead."
    )


def resolve_parameters(
    config: Any = None,
    profile: Optional[str] = None,
    carbon_price: Optional[float] = None,
) -> ValueParameters:
    """Resolve the EV parameters of a profile, with an optional carbon-price override.

    An unknown configured profile falls back to ``balanced`` with a warning; an
    explicit ``carbon_price`` replaces the profile's carbon price.
    """
    configured = getattr(config, "RECOMMENDATION_RANKING_PROFILE", None) if config is not None else None
    try:
        name = normalize_profile(profile or configured)
    except ValueError:
        if profile:
            raise
        logger.warning("Unknown RECOMMENDATION_RANKING_PROFILE %r; using %s.", configured, DEFAULT_PROFILE)
        name = DEFAULT_PROFILE
    preset = PROFILES[name]
    base_price = _config_float(config, "CARBON_PRICE_PER_TONNE", 100.0)
    price = float(carbon_price) if carbon_price is not None else base_price * preset.carbon_price_multiplier
    if not math.isfinite(price) or price < 0:
        raise ValueError("carbon_price must be a finite, non-negative number.")
    _warn_deprecated_weights(config)
    hour_cost = max(_config_float(config, "RECOMMENDATION_ENGINEER_HOUR_COST", 0.0), preset.engineer_hour_cost_floor)
    return ValueParameters(
        profile=name,
        carbon_price_per_tonne=price,
        risk_aversion=preset.risk_aversion,
        effort_weight=preset.effort_weight,
        engineer_hour_cost=hour_cost,
        incident_cost=_config_float(config, "RECOMMENDATION_INCIDENT_COST", 100.0),
        capacity_value_weight=_config_float(config, "RECOMMENDATION_CAPACITY_VALUE_WEIGHT", 1.0),
        currency=str(getattr(config, "IMPACT_PRICING_CURRENCY", "USD") or "USD") if config is not None else "USD",
    )


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value.value if hasattr(value, "value") else value)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _projected_quantities(rec: Any) -> tuple:
    """(cost_annual, co2e_annual_g, currency or None) from the Impact projection, else the legacy columns."""
    impact = getattr(rec, "impact", None)
    if isinstance(impact, Mapping) and _finite(impact.get("expected_billable_cost_annual")) is not None:
        cost = _finite(impact.get("expected_billable_cost_annual")) or 0.0
        co2e = _finite(impact.get("co2e_expected_annual_g")) or 0.0
        if impact.get("co2e_modeled") is False:
            co2e = 0.0
        return cost, co2e, impact.get("currency") or None
    cost = _finite(getattr(rec, "potential_savings_cost", None)) or 0.0
    co2e = _finite(getattr(rec, "potential_savings_co2e_grams", None)) or 0.0
    return cost, co2e, None


def _allocated_cost(rec: Any) -> Optional[float]:
    """Allocated annual cost of the Impact projection (what the workload stops being charged), or None."""
    impact = getattr(rec, "impact", None)
    if not isinstance(impact, Mapping):
        return None
    return _finite(impact.get("allocated_cost_annual"))


def proposes_request_increase(rec: Any) -> bool:
    """True when a CPU or memory recommendation raises the current request (a reliability change, H1)."""
    rec_type = _text(getattr(rec, "type", None))
    if rec_type == "RIGHTSIZING_CPU":
        current = getattr(rec, "current_cpu_request_millicores", None)
        proposed = getattr(rec, "recommended_cpu_request_millicores", None)
    elif rec_type == "RIGHTSIZING_MEMORY":
        current = getattr(rec, "current_memory_request_bytes", None)
        proposed = getattr(rec, "recommended_memory_request_bytes", None)
    else:
        return False
    before, after = _finite(current), _finite(proposed)
    return before is not None and after is not None and after > before


def success_probability(
    confidence: Optional[float],
    risk_level: str,
    track_record: Any = None,
) -> tuple:
    """Beta posterior mean of success, scaled by the risk class. Returns ``(p, alpha, beta)``.

    The analyzer's confidence is a prior worth ``CONFIDENCE_PRIOR_STRENGTH``
    observations; each measured outcome of the source family on this capability
    moves it (realized adds to alpha, not realized or rolled back to beta).
    """
    prior = _finite(confidence)
    prior = 0.5 if prior is None else min(max(prior, 0.05), 0.95)
    alpha = prior * CONFIDENCE_PRIOR_STRENGTH
    beta = (1.0 - prior) * CONFIDENCE_PRIOR_STRENGTH
    if track_record is not None:
        alpha += track_record.realized
        beta += track_record.failures
    p = alpha / (alpha + beta) * RISK_SUCCESS_FACTOR.get(risk_level, RISK_SUCCESS_FACTOR["medium"])
    return min(max(p, 0.01), 0.99), alpha, beta


def blast_radius(scope: str, risk_factors: Sequence[str], criticality: Optional[str]) -> float:
    radius = SCOPE_BLAST.get(scope, 1.0)
    radius *= CRITICALITY_BLAST.get(criticality or "normal", 1.0)
    if "singleton_workload" in risk_factors:
        radius *= SINGLETON_BLAST
    return radius


def _criticality_from_factors(risk_factors: Sequence[str]) -> Optional[str]:
    for factor in risk_factors:
        if factor.startswith("criticality_"):
            return normalize_criticality(factor[len("criticality_") :])
    return None


def default_delivery_mode(rec: Any) -> str:
    """Delivery assumed at scan time: a PR for a container request decrease with a container target.

    Request increases (reliability, E9) are advisory until container resources
    v2: GitOps actions are built for decreases only, so they are delivered manually.
    """
    rec_type = _text(getattr(rec, "type", None))
    if rec_type == "RIGHTSIZING_CPU":
        current, proposed = (
            getattr(rec, "current_cpu_request_millicores", None),
            getattr(rec, "recommended_cpu_request_millicores", None),
        )
    elif rec_type == "RIGHTSIZING_MEMORY":
        current, proposed = (
            getattr(rec, "current_memory_request_bytes", None),
            getattr(rec, "recommended_memory_request_bytes", None),
        )
    else:
        return "manual"
    increase = current is not None and proposed is not None and proposed > current
    if not increase and all(
        getattr(rec, name, None) for name in ("namespace", "owner_kind", "owner_name", "container_name")
    ):
        return "direct"
    return "manual"


def compute_value(
    rec: Any,
    params: ValueParameters,
    track_record: Any = None,
    *,
    delivery_mode: Optional[str] = None,
    criticality: Optional[str] = None,
) -> Dict[str, Any]:
    """The ValueScore of one recommendation (a JSON-safe dict)."""
    cost, co2e_g, impact_currency = _projected_quantities(rec)
    carbon_value = params.carbon_price_per_tonne * co2e_g / 1e6
    # IMP-03: on fixed nodes the billable projection is 0 for every request change, so the value
    # also counts the allocated cost the change releases (or reserves), with a documented weight.
    allocated = _allocated_cost(rec)
    released_value = params.capacity_value_weight * (allocated - cost) if allocated is not None else 0.0
    value_annual = cost + carbon_value + released_value

    risk_level = _text(getattr(rec, "risk_level", None), "medium") or "medium"
    risk_factors = [str(f) for f in getattr(rec, "risk_factors", None) or []]
    p, alpha, beta = success_probability(getattr(rec, "confidence", None), risk_level, track_record)

    crit = normalize_criticality(criticality) or _criticality_from_factors(risk_factors)
    radius = blast_radius(_text(getattr(rec, "scope", None), "pod"), risk_factors, crit)
    severity = INCIDENT_SEVERITY.get(risk_level, INCIDENT_SEVERITY["medium"])
    incident_cost = params.risk_aversion * params.incident_cost * severity * radius

    # The lane follows the direction of the change, never the sign of a realization-scaled number.
    increase = proposes_request_increase(rec) or (allocated is not None and allocated < 0)
    lane = "reliability" if increase or value_annual < 0 else "savings"
    incident_cost_avoided = 0.0
    p_incident_now = None
    if lane == "reliability":
        history = any(factor in risk_factors for factor in _HISTORY_FACTORS)
        p_incident_now = INCIDENT_PROBABILITY_WITH_HISTORY if history else INCIDENT_PROBABILITY_BASELINE
        incident_cost_avoided = p_incident_now * params.incident_cost * radius

    mode = delivery_mode or default_delivery_mode(rec)
    effort = _text(getattr(rec, "effort", None), "low") or "low"
    hours = EFFORT_HOURS.get(effort, EFFORT_HOURS["medium"]) * (DIRECT_DELIVERY_EFFORT if mode == "direct" else 1.0)
    effort_cost = hours * params.engineer_hour_cost * params.effort_weight

    ev = p * (value_annual + incident_cost_avoided) - (1.0 - p) * incident_cost - effort_cost
    factors = {
        "cost": round(p * cost, 2),
        "carbon": round(p * carbon_value, 2),
        "incident_risk": round(-(1.0 - p) * incident_cost, 2),
        "effort": round(-effort_cost, 2),
    }
    if released_value:
        factors["released_capacity"] = round(p * released_value, 2)
    if incident_cost_avoided:
        factors["reliability"] = round(p * incident_cost_avoided, 2)

    return {
        "model": VALUE_MODEL,
        "method": "projected",
        "profile": params.profile,
        "currency": impact_currency or params.currency,
        "lane": lane,
        "value_annual": round(value_annual, 4),
        "p_success": round(p, 4),
        "incident_cost": round(incident_cost, 4),
        "incident_cost_avoided": round(incident_cost_avoided, 4),
        "effort_cost": round(effort_cost, 4),
        "ev": round(ev, 2),
        "factors": factors,
        "components": {
            "cost_annual": round(cost, 4),
            "billable_cost_annual": round(cost, 4),
            "allocated_cost_annual": round(allocated, 4) if allocated is not None else None,
            "capacity_value_weight": params.capacity_value_weight,
            "released_capacity_value_annual": round(released_value, 4),
            "co2e_annual_g": round(co2e_g, 4),
            "carbon_price_per_tonne": params.carbon_price_per_tonne,
            "carbon_value_annual": round(carbon_value, 4),
            "confidence": _finite(getattr(rec, "confidence", None)),
            # IMP-03: the projection's own input confidence (price tier, short coverage), carried apart.
            "projection_confidence": (
                _finite(rec.impact.get("confidence")) if isinstance(getattr(rec, "impact", None), Mapping) else None
            ),
            "risk_level": risk_level,
            "risk_success_factor": RISK_SUCCESS_FACTOR.get(risk_level, RISK_SUCCESS_FACTOR["medium"]),
            "track_record": (
                {
                    "source_family": track_record.source_family,
                    "capability": track_record.capability,
                    "realized": track_record.realized,
                    "failed": track_record.failures,
                }
                if track_record is not None
                else None
            ),
            "posterior_alpha": round(alpha, 4),
            "posterior_beta": round(beta, 4),
            "risk_aversion": params.risk_aversion,
            "incident_severity": severity,
            "blast_radius": radius,
            "criticality": crit,
            "p_incident_now": p_incident_now,
            "effort": effort,
            "effort_hours": hours,
            "engineer_hour_cost": params.engineer_hour_cost,
            "effort_weight": params.effort_weight,
            "delivery_mode": mode,
        },
    }


def ranking_sort_key(rec: Any) -> tuple:
    """Total, filter-independent order: EV, then value, then projected cost, then identity."""
    value = getattr(rec, "value", None) or {}
    ev = _finite(value.get("ev")) if isinstance(value, Mapping) else None
    if ev is None:
        ev = _finite(getattr(rec, "ranking_score", None))
    value_annual = _finite(value.get("value_annual")) if isinstance(value, Mapping) else None
    cost = _finite(getattr(rec, "potential_savings_cost", None)) or 0.0
    return (
        0 if ev is not None else 1,
        -(ev or 0.0),
        -(value_annual or 0.0),
        -cost,
        str(getattr(rec, "fingerprint", None) or ""),
        int(getattr(rec, "id", None) or 0),
    )


def sort_by_value(recommendations: Sequence[Any]) -> List[Any]:
    return sorted(recommendations, key=ranking_sort_key)


def score_recommendations(
    recommendations: Sequence[Any],
    profile: Optional[str] = None,
    *,
    config: Any = None,
    carbon_price: Optional[float] = None,
    track_record: Optional[TrackRecordBook] = None,
    delivery_modes: Optional[Mapping[int, str]] = None,
    criticality: Optional[Mapping[int, Optional[str]]] = None,
    params: Optional[ValueParameters] = None,
) -> List[Any]:
    """Attach ``value``, ``ranking_score`` (EV) and ``ranking_factors`` (currency) to each recommendation.

    Accepts ``Recommendation``, ``RecommendationRecord`` and ``TopRecommendation``-like
    objects; ``model_copy`` preserves the concrete type. Each score depends only
    on its own recommendation, so scores never change with the batch.
    """
    if not recommendations:
        return []
    resolved = params or resolve_parameters(config, profile, carbon_price)
    book = track_record or EMPTY_BOOK
    scored: List[Any] = []
    for rec in recommendations:
        rec_id = getattr(rec, "id", None)
        value = compute_value(
            rec,
            resolved,
            book.get(getattr(rec, "source", None), getattr(rec, "capability", None)),
            delivery_mode=(delivery_modes or {}).get(rec_id) if rec_id is not None else None,
            criticality=(criticality or {}).get(rec_id) if rec_id is not None else None,
        )
        factors = value.pop("factors")
        scored.append(rec.model_copy(update={"value": value, "ranking_score": value["ev"], "ranking_factors": factors}))
    return scored
