# src/greenkube/core/optimization/risks.py
"""Risk, confidence and effort assessment for recommendations."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import EffortLevel, Recommendation, RecommendationSource, RecommendationType, RiskLevel

if TYPE_CHECKING:
    from greenkube.core.config import Config


@dataclass
class RiskAssessment:
    """Outcome of the risk/confidence/effort assessment."""

    risk_level: RiskLevel = RiskLevel.MEDIUM
    risk_factors: List[str] = field(default_factory=list)
    confidence: float = 0.5
    confidence_factors: dict = field(default_factory=dict)
    effort: EffortLevel = EffortLevel.LOW
    reversible: bool = True
    requires_restart: bool = False


def _base_confidence(rec: Recommendation, evidence: Optional[RecommendationEvidence], config: "Config") -> tuple:
    """Aggregates sample coverage, estimation flags and source authority."""
    score = 0.4
    factors: dict = {"base": 0.4}

    if evidence is not None:
        min_samples = getattr(config, "RECOMMENDATION_MIN_SAMPLES", 36)
        if evidence.sample_count >= min_samples:
            score += 0.25
            factors["samples"] = 0.25
        elif evidence.sample_count > 0:
            score += 0.1 * (evidence.sample_count / min_samples)
            factors["samples"] = round(0.1 * (evidence.sample_count / min_samples), 3)

        if evidence.coverage_ratio >= 0.9:
            score += 0.15
            factors["coverage"] = 0.15
        else:
            factors["coverage"] = 0.0

    if rec.source == RecommendationSource.VPA:
        score += 0.1
        factors["source_authority"] = 0.1
    elif rec.source == RecommendationSource.KARPENTER:
        score += 0.05
        factors["source_authority"] = 0.05

    return max(0.0, min(1.0, score)), factors


def _assess_rightsizing(rec: Recommendation, evidence: Optional[RecommendationEvidence]) -> tuple:
    """Risk of a CPU/memory rightsizing change."""
    if evidence is None:
        return RiskLevel.MEDIUM, ["insufficient_evidence"]

    if rec.type == RecommendationType.RIGHTSIZING_CPU:
        stats = evidence.cpu_usage
        proposed = evidence.proposed.cpu_request_millicores
        if stats and proposed:
            if stats.max >= proposed:
                return RiskLevel.HIGH, ["observed_max_exceeds_proposal"]
            if stats.p99 >= proposed * 0.9:
                return RiskLevel.MEDIUM, ["p99_close_to_proposal"]
            return RiskLevel.LOW, ["sufficient_headroom"]
        return RiskLevel.MEDIUM, ["missing_cpu_usage"]

    # RIGHTSIZING_MEMORY
    if evidence.restart_count:
        return RiskLevel.HIGH, ["restart_history"]
    stats = evidence.memory_usage
    proposed_mem = evidence.proposed.memory_request_bytes
    if stats and proposed_mem:
        if stats.max >= proposed_mem:
            return RiskLevel.HIGH, ["observed_max_exceeds_proposal"]
        if stats.p95 >= proposed_mem * 0.9:
            return RiskLevel.MEDIUM, ["p95_close_to_proposal"]
        return RiskLevel.LOW, ["sufficient_headroom"]
    return RiskLevel.MEDIUM, ["missing_memory_usage"]


def assess(rec: Recommendation, evidence: Optional[RecommendationEvidence], config: "Config") -> RiskAssessment:
    """Computes risk level, factors, confidence and effort for a recommendation."""
    confidence, confidence_factors = _base_confidence(rec, evidence, config)
    assessment = RiskAssessment(confidence=confidence, confidence_factors=confidence_factors)

    if rec.type in (RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY):
        assessment.risk_level, assessment.risk_factors = _assess_rightsizing(rec, evidence)
        assessment.effort = EffortLevel.LOW
        assessment.reversible = True
        assessment.requires_restart = True
    elif rec.type == RecommendationType.AUTOSCALING_CANDIDATE:
        assessment.risk_level, assessment.risk_factors = RiskLevel.LOW, ["additive_autoscaling"]
        assessment.effort = EffortLevel.MEDIUM
        assessment.reversible = True
    elif rec.type == RecommendationType.OFF_PEAK_SCALING:
        assessment.risk_level, assessment.risk_factors = RiskLevel.MEDIUM, ["availability_window"]
        assessment.effort = EffortLevel.MEDIUM
        assessment.reversible = True
    elif rec.type == RecommendationType.CARBON_AWARE_SCHEDULING:
        assessment.risk_level, assessment.risk_factors = RiskLevel.LOW, ["scheduling_only"]
        assessment.effort = EffortLevel.MEDIUM
        assessment.reversible = True
    elif rec.type == RecommendationType.ZOMBIE_POD:
        assessment.risk_level, assessment.risk_factors = RiskLevel.LOW, ["no_observed_usage"]
        assessment.effort = EffortLevel.LOW
        assessment.reversible = False
    elif rec.type == RecommendationType.IDLE_NAMESPACE:
        assessment.risk_level, assessment.risk_factors = RiskLevel.MEDIUM, ["namespace_decommission"]
        assessment.effort = EffortLevel.MEDIUM
        assessment.reversible = False
    elif rec.type in (RecommendationType.OVERPROVISIONED_NODE, RecommendationType.UNDERUTILIZED_NODE):
        assessment.risk_level, assessment.risk_factors = RiskLevel.HIGH, ["blast_radius_multi_workload"]
        assessment.effort = EffortLevel.HIGH
        assessment.reversible = True
        assessment.requires_restart = True
    elif rec.type in (
        RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        RecommendationType.ORPHANED_LOAD_BALANCER,
    ):
        assessment.risk_level, assessment.risk_factors = RiskLevel.LOW, ["no_bound_consumer"]
        assessment.effort = EffortLevel.LOW
        assessment.reversible = False

    return assessment
