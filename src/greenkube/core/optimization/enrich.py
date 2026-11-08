# src/greenkube/core/optimization/enrich.py
"""Enriches finalized recommendations with evidence, risk and expected value."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from greenkube.core.optimization.evidence import build_evidence, build_patch, series_for
from greenkube.core.optimization.risks import WorkloadRiskProfile, assess
from greenkube.core.optimization.scoring import score_recommendations
from greenkube.core.optimization.statistics import percentile
from greenkube.core.optimization.track_record import TrackRecordBook, load_track_record
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationPatchV2

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)


def _replica_p95_max(series: Sequence[CombinedMetric], attr: str) -> Optional[float]:
    """Highest per-replica p95 of a usage signal (per-replica headroom, I4)."""
    by_pod: Dict[str, List[float]] = defaultdict(list)
    for metric in series:
        value = getattr(metric, attr, None)
        if value is not None:
            by_pod[metric.pod_name].append(float(value))
    if not by_pod:
        return None
    return max(percentile(values, 95) for values in by_pod.values())


def _current_replicas(series: Sequence[CombinedMetric]) -> Optional[int]:
    """Replicas observed at the latest timestamp; unknown without timestamps."""
    stamped = [metric for metric in series if metric.timestamp is not None]
    if not stamped:
        return None
    latest = max(metric.timestamp for metric in stamped if metric.timestamp is not None)
    pods = {metric.pod_name for metric in stamped if metric.timestamp == latest}
    return len(pods) or None


def build_risk_profile(rec: Recommendation, context: "OptimizationContext") -> WorkloadRiskProfile:
    """Workload facts available to a scan: replicas, per-replica headroom and HPA presence."""
    series = series_for(rec, context) if not rec.target_node else []
    hpa_resources = None
    if context.hpa_targets is not None and rec.namespace and rec.owner_kind and rec.owner_name:
        if (rec.namespace, rec.owner_kind, rec.owner_name) in context.hpa_targets:
            # The HPA collector reports targets, not their metrics.
            hpa_resources = frozenset({"*"})
        else:
            hpa_resources = frozenset()
    return WorkloadRiskProfile(
        replicas=_current_replicas(series) if rec.owner_kind else None,
        replica_cpu_p95_max=_replica_p95_max(series, "cpu_usage_millicores"),
        replica_memory_p95_max=_replica_p95_max(series, "memory_usage_bytes"),
        hpa_resources=hpa_resources,
    )


def _enrich_one(rec: Recommendation, context: "OptimizationContext", config: "Config") -> Recommendation:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=getattr(config, "RECOMMENDATION_TTL_DAYS", 14))

    evidence = build_evidence(rec, context)
    patch_payload = build_patch(rec)
    patch = RecommendationPatchV2.model_validate(patch_payload) if patch_payload is not None else None
    assessment = assess(rec, evidence, config, build_risk_profile(rec, context))

    if evidence is not None:
        evidence = evidence.model_copy(
            update={
                "risk_level": assessment.risk_level.value,
                "risk_factors": list(assessment.risk_factors),
                "confidence": assessment.confidence,
                "confidence_factors": dict(assessment.confidence_factors),
                "expires_at": expires_at,
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
    track_record: Optional[TrackRecordBook] = None,
) -> List[Recommendation]:
    """Adds evidence, risk assessment and the expected value (EV) to each recommendation."""
    enriched = [_enrich_one(rec, context, config) for rec in recommendations]
    return score_recommendations(enriched, config=config, track_record=track_record)


async def rescore_with_track_record(
    recommendations: Sequence[Any],
    reco_repo: Any,
    config: "Config",
) -> List[Any]:
    """Re-scores a scan's output with the measured track record of each source (I3).

    The track record lives in the database, so scans score with priors first and
    the engine calls this before persisting.
    """
    if not recommendations:
        return list(recommendations)
    book = await load_track_record(reco_repo)
    if not book.records:
        return list(recommendations)
    return score_recommendations(recommendations, config=config, track_record=book)
