# src/greenkube/core/optimization/enrich.py
"""Enriches finalized recommendations with evidence, risk and ranking."""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Sequence

from greenkube.core.optimization.evidence import build_evidence, build_patch
from greenkube.core.optimization.risks import assess
from greenkube.core.optimization.scoring import score_recommendations
from greenkube.models.metrics import Recommendation

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)


def _enrich_one(rec: Recommendation, context: "OptimizationContext", config: "Config") -> Recommendation:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=getattr(config, "RECOMMENDATION_TTL_DAYS", 14))

    evidence = build_evidence(rec, context)
    patch = build_patch(rec)
    assessment = assess(rec, evidence, config)

    if evidence is not None:
        evidence = evidence.model_copy(
            update={
                "risk_level": assessment.risk_level.value,
                "risk_factors": list(assessment.risk_factors),
                "confidence": assessment.confidence,
                "confidence_factors": dict(assessment.confidence_factors),
                "expires_at": expires_at,
                "proposed_patch": patch,
            }
        )

    return rec.model_copy(
        update={
            "evidence": evidence,
            "risk_level": assessment.risk_level,
            "risk_factors": list(assessment.risk_factors),
            "confidence": assessment.confidence,
            "effort": assessment.effort,
            "patch": patch,
            "expires_at": expires_at,
            "reversible": assessment.reversible,
            "requires_restart": assessment.requires_restart,
        }
    )


def enrich_recommendations(
    recommendations: Sequence[Recommendation],
    context: "OptimizationContext",
    config: "Config",
) -> List[Recommendation]:
    """Adds evidence, risk assessment and ranking scores to each recommendation."""
    enriched = [_enrich_one(rec, context, config) for rec in recommendations]
    return score_recommendations(
        enriched,
        profile=getattr(config, "RECOMMENDATION_RANKING_PROFILE", "balanced"),
        weights_override=getattr(config, "RECOMMENDATION_RANKING_WEIGHTS", ""),
    )
