# src/greenkube/core/optimization/scoring.py
"""Multi-criteria ranking of recommendations.

The score combines projected impact (carbon and cost), confidence, reliability
risk, implementation effort, actionability and source authority. Profiles make
the trade-off explicit; the factor breakdown makes the rank explainable.
"""

import json
import logging
import math
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

PROFILE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "balanced": {
        "carbon": 0.30,
        "cost": 0.30,
        "confidence": 0.15,
        "risk": 0.15,
        "effort": 0.05,
        "actionability": 0.05,
        "source": 0.00,
    },
    "carbon_first": {
        "carbon": 0.55,
        "cost": 0.10,
        "confidence": 0.15,
        "risk": 0.10,
        "effort": 0.05,
        "actionability": 0.05,
        "source": 0.00,
    },
    "cost_first": {
        "carbon": 0.10,
        "cost": 0.55,
        "confidence": 0.15,
        "risk": 0.10,
        "effort": 0.05,
        "actionability": 0.05,
        "source": 0.00,
    },
    "quick_wins": {
        "carbon": 0.10,
        "cost": 0.15,
        "confidence": 0.20,
        "risk": 0.15,
        "effort": 0.30,
        "actionability": 0.10,
        "source": 0.00,
    },
    "low_risk": {
        "carbon": 0.15,
        "cost": 0.15,
        "confidence": 0.20,
        "risk": 0.50,
        "effort": 0.00,
        "actionability": 0.00,
        "source": 0.00,
    },
}

DEFAULT_PROFILE = "balanced"

_RISK_WEIGHT = {"low": 0.0, "medium": 0.5, "high": 1.0}
_EFFORT_WEIGHT = {"low": 0.0, "medium": 0.5, "high": 1.0}
_SOURCE_AUTHORITY = {"vpa": 1.0, "karpenter": 0.8, "greenkube": 0.5}


def _value(value, default: str = "") -> str:
    if value is None:
        return default
    return value.value if hasattr(value, "value") else str(value)


def _log_minmax(values: Sequence[float]) -> List[float]:
    """log1p then min-max normalization; returns zeros when the range is flat."""
    logged = [math.log1p(max(float(v), 0.0)) for v in values]
    low, high = min(logged), max(logged)
    if high - low < 1e-12:
        return [0.0 for _ in values]
    return [(v - low) / (high - low) for v in logged]


def resolve_weights(profile: Optional[str], override_json: Optional[str] = None) -> Dict[str, float]:
    """Returns the weights for a profile, applying an optional JSON override."""
    weights = dict(PROFILE_WEIGHTS.get((profile or DEFAULT_PROFILE).strip().lower(), PROFILE_WEIGHTS[DEFAULT_PROFILE]))
    if override_json:
        try:
            overrides = json.loads(override_json)
            if isinstance(overrides, dict):
                for key, value in overrides.items():
                    if key in weights and isinstance(value, (int, float)):
                        weights[key] = float(value)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid RECOMMENDATION_RANKING_WEIGHTS JSON override.")
    return weights


def score_recommendations(
    recommendations: Sequence[Any],
    profile: Optional[str] = None,
    weights_override: Optional[str] = None,
) -> List[Any]:
    """Computes and attaches ranking_score/ranking_factors to each recommendation.

    Accepts both ``Recommendation`` and ``RecommendationRecord`` instances: only
    shared attributes are read, and ``model_copy`` preserves the concrete type.
    """
    if not recommendations:
        return []

    weights = resolve_weights(profile, weights_override)

    carbon_norm = _log_minmax([float(r.potential_savings_co2e_grams or 0.0) for r in recommendations])
    cost_norm = _log_minmax([float(r.potential_savings_cost or 0.0) for r in recommendations])

    scored: List[Any] = []
    for index, rec in enumerate(recommendations):
        risk = _RISK_WEIGHT.get(_value(rec.risk_level, "medium"), 0.5)
        effort = _EFFORT_WEIGHT.get(_value(rec.effort, "low"), 0.0)
        authority = _SOURCE_AUTHORITY.get(_value(rec.source, "greenkube"), 0.5)
        confidence = float(rec.confidence or 0.0)

        factors = {
            "carbon": round(weights["carbon"] * carbon_norm[index], 4),
            "cost": round(weights["cost"] * cost_norm[index], 4),
            "confidence": round(weights["confidence"] * confidence, 4),
            "risk": round(weights["risk"] * (1.0 - risk), 4),
            "effort": round(weights["effort"] * (1.0 - effort), 4),
            "actionability": round(weights["actionability"] * (1.0 if rec.patch else 0.0), 4),
            "source": round(weights["source"] * authority, 4),
        }
        score = round(sum(factors.values()), 4)
        scored.append(rec.model_copy(update={"ranking_score": score, "ranking_factors": factors}))

    return scored
