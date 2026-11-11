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
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationPatchV2, RecommendationType

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
    """Replicas observed in the latest sampling step; unknown without timestamps.

    Replicas are rarely scraped at the very same instant, so a pod counts when its
    last sample falls within half a sampling interval of the latest sample. Half an
    interval absorbs scrape jitter but excludes a pod replaced by a rollout one step
    earlier. The interval is the median gap between a pod's consecutive samples.
    """
    by_pod: Dict[str, List[datetime]] = defaultdict(list)
    for metric in series:
        if metric.timestamp is not None:
            by_pod[metric.pod_name].append(metric.timestamp)
    if not by_pod:
        return None
    gaps: List[float] = []
    for stamps in by_pod.values():
        ordered = sorted(set(stamps))
        gaps.extend((later - earlier).total_seconds() for earlier, later in zip(ordered, ordered[1:]))
    tolerance = timedelta(seconds=percentile(gaps, 50) / 2) if gaps else timedelta(0)
    last_seen = {pod: max(stamps) for pod, stamps in by_pod.items()}
    latest = max(last_seen.values())
    return sum(1 for seen in last_seen.values() if latest - seen <= tolerance) or None


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
    summary = _usage_summary(rec, context)
    return WorkloadRiskProfile(
        replicas=_current_replicas(series) if rec.owner_kind else None,
        replica_cpu_p95_max=_replica_p95_max(series, "cpu_usage_millicores"),
        replica_memory_p95_max=_replica_p95_max(series, "memory_usage_bytes"),
        oom_events=summary.oom_kill_count if summary is not None else None,
        cpu_throttling_ratio=summary.throttling_ratio if summary is not None else None,
        hpa_resources=hpa_resources,
        jvm=True if summary is not None and summary.jvm_hint else None,
    )


def _usage_summary(rec: Recommendation, context: "OptimizationContext") -> Optional[Any]:
    """The usage summary of the recommendation's target container, with its reliability signals (E9)."""
    if not rec.namespace or rec.target_node or context.usage is None:
        return None
    if rec.owner_kind and rec.owner_name:
        target = (rec.namespace, rec.owner_kind, rec.owner_name)
    elif rec.pod_name:
        target = (rec.namespace, "Pod", rec.pod_name)
    else:
        return None
    return context.usage.summaries.get(target, {}).get(rec.container_name or "")


#: Confidence multiplier of a memory decrease on a container suspected to run a JVM.
JVM_MEMORY_DECREASE_CONFIDENCE = 0.5


def _memory_decrease(rec: Recommendation) -> bool:
    current, proposed = rec.current_memory_request_bytes, rec.recommended_memory_request_bytes
    return current is not None and proposed is not None and proposed < current


def _enrich_one(rec: Recommendation, context: "OptimizationContext", config: "Config") -> Recommendation:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=getattr(config, "RECOMMENDATION_TTL_DAYS", 14))

    evidence = build_evidence(rec, context)
    patch_payload = build_patch(rec)
    patch = RecommendationPatchV2.model_validate(patch_payload) if patch_payload is not None else None
    profile = build_risk_profile(rec, context)
    assessment = assess(rec, evidence, config, profile)
    if profile.jvm and rec.type == RecommendationType.RIGHTSIZING_MEMORY and _memory_decrease(rec):
        # The JVM sizes its heap itself: the working set does not show what it needs (E9).
        assessment.confidence = round(assessment.confidence * JVM_MEMORY_DECREASE_CONFIDENCE, 4)
        assessment.confidence_factors = {
            **assessment.confidence_factors,
            "jvm_suspected": JVM_MEMORY_DECREASE_CONFIDENCE,
        }

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
