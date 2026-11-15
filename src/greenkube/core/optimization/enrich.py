# src/greenkube/core/optimization/enrich.py
"""Enriches finalized recommendations with evidence, risk and expected value."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from greenkube.core.optimization.evidence import build_evidence, build_patch, series_for, usage_summary_for
from greenkube.core.optimization.risks import JVM_MEMORY_DECREASE_CONFIDENCE, WorkloadRiskProfile, assess
from greenkube.core.optimization.scoring import score_recommendations
from greenkube.core.optimization.statistics import percentile
from greenkube.core.optimization.track_record import TrackRecordBook, load_track_record
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationPatchV2
from greenkube.models.workload_usage import POD_LEVEL

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)

__all__ = [
    "JVM_MEMORY_DECREASE_CONFIDENCE",
    "build_risk_profile",
    "enrich_recommendations",
    "rescore_with_track_record",
]


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


def _oom_events(summary: Any, context: "OptimizationContext") -> Optional[int]:
    """OOM kills of the target, or None when unknown (:func:`known_oom_events`)."""
    from greenkube.core.optimization.analyzers.rightsizing import known_oom_events

    if summary is None:
        return None
    pod_summary = context.usage_snapshot().summaries.get(summary.target, {}).get(POD_LEVEL)
    return known_oom_events(summary, pod_summary)


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
    summary = usage_summary_for(rec, context)
    oom_events = _oom_events(summary, context)
    oom_unknown_cause = None
    if oom_events is None:
        from greenkube.core.optimization.analyzers.rightsizing import oom_unknown_cause as _cause

        oom_unknown_cause = _cause(summary, context.usage_snapshot().reliability_signal_status)
    return WorkloadRiskProfile(
        replicas=_current_replicas(series) if rec.owner_kind else None,
        replica_cpu_p95_max=_replica_p95_max(series, "cpu_usage_millicores"),
        replica_memory_p95_max=_replica_p95_max(series, "memory_usage_bytes"),
        oom_events=oom_events,
        oom_unknown_cause=oom_unknown_cause,
        cpu_throttling_ratio=summary.throttling_ratio if summary is not None else None,
        hpa_resources=hpa_resources,
        jvm=True if summary is not None and summary.jvm_hint else None,
    )


def _enrich_one(rec: Recommendation, context: "OptimizationContext", config: "Config") -> Recommendation:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=getattr(config, "RECOMMENDATION_TTL_DAYS", 14))

    evidence = build_evidence(rec, context)
    patch_payload = build_patch(rec)
    patch = RecommendationPatchV2.model_validate(patch_payload) if patch_payload is not None else None
    profile = build_risk_profile(rec, context)
    assessment = assess(rec, evidence, config, profile)

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
