# src/greenkube/core/optimization/risks.py
"""Risk, confidence and effort assessment for recommendations (risk model v2, roadmap I4).

The base risk of a rightsizing change compares the pooled usage distribution
with the proposal. Risk model v2 then folds in what the pooled numbers hide or
cannot know, from a :class:`WorkloadRiskProfile`:

* per-replica headroom: the hottest replica, not the pool, must fit the request;
* OOM and CPU-throttling history (it raises the risk of a decrease; on a
  reliability increase it is the incident the change avoids, so it is recorded
  as a factor without raising the risk);
* a singleton workload (one replica): a restart is an outage;
* HPA, VPA and PDB interplay;
* a JVM flag: heap sizing hides memory needs from the working set;
* in-place resize constraints (memory decreases are best effort, the static
  CPU manager is incompatible with CPU resizes);
* the ``greenkube.io/criticality`` label.

A suspected JVM also halves the confidence of a memory decrease
(:data:`JVM_MEMORY_DECREASE_CONFIDENCE`). Deletions (zombie pods, idle
namespaces, orphaned volumes and load balancers) cannot be undone by a revert,
so they are never low risk and always carry ``irreversible_deletion``.

Unknown profile facts (``None``) never change the risk level, with one
exception: an unknown OOM history (``oom_events is None``) makes a memory
decrease at least medium risk with the factor ``oom_history_unknown``, which
blocks automation (REC-04: never treat unknown as healthy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, FrozenSet, List, Mapping, Optional

from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import EffortLevel, Recommendation, RecommendationSource, RecommendationType, RiskLevel

if TYPE_CHECKING:
    from greenkube.core.config import Config

#: Workload label that declares business criticality: critical, high, normal or low.
CRITICALITY_LABEL = "greenkube.io/criticality"
#: Label or annotation flagging a JVM workload (``"true"``); ``greenkube.io/runtime: jvm`` also works.
JVM_FLAG = "greenkube.io/jvm"
RUNTIME_LABEL = "greenkube.io/runtime"

CRITICALITY_LEVELS = ("critical", "high", "normal", "low")

#: VPA update modes in which the VPA writes pod requests itself (a conflicting writer).
CONFLICTING_VPA_UPDATE_MODES = frozenset({"Auto", "Recreate", "InPlace", "InPlaceOrRecreate"})

#: Confidence multiplier of a memory decrease on a container suspected to run a JVM.
JVM_MEMORY_DECREASE_CONFIDENCE = 0.5

#: Risk factor of every recommendation that deletes something (no revert brings it back).
IRREVERSIBLE_DELETION = "irreversible_deletion"

#: Risk factor of a memory decrease whose OOM history could not be read (blocks automation).
OOM_HISTORY_UNKNOWN = "oom_history_unknown"
#: Prefix of the factor naming why the OOM history is unknown
#: (``oom_unknown_no_usage_summary``, ``oom_unknown_signal_failed``, ``oom_unknown_signal_unavailable``).
OOM_UNKNOWN_CAUSE_PREFIX = "oom_unknown_"

#: CPU throttling ratio above which a CPU decrease is at least medium (high above the second value).
THROTTLING_MEDIUM = 0.05
THROTTLING_HIGH = 0.25

_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
_BY_RANK = {rank: level for level, rank in _ORDER.items()}
_RIGHTSIZING = (RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY)


@dataclass(frozen=True)
class WorkloadRiskProfile:
    """Facts about the target workload that the pooled usage statistics cannot show.

    Every field is optional: ``None`` means "not observed", never "safe".
    """

    replicas: Optional[int] = None
    #: Highest per-replica p95 usage over the window (millicores / bytes).
    replica_cpu_p95_max: Optional[float] = None
    replica_memory_p95_max: Optional[float] = None
    oom_events: Optional[int] = None
    #: Why ``oom_events`` is None (``no_usage_summary``, ``signal_failed``, ``signal_unavailable``).
    oom_unknown_cause: Optional[str] = None
    cpu_throttling_ratio: Optional[float] = None
    #: Resources an HPA scales on (``cpu``, ``memory``); ``{"*"}`` when an HPA exists but its metrics are unknown.
    hpa_resources: Optional[FrozenSet[str]] = None
    vpa_update_mode: Optional[str] = None
    pdb_conflict: Optional[bool] = None
    criticality: Optional[str] = None
    jvm: Optional[bool] = None
    static_cpu_manager: Optional[bool] = None


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


def _at_least(current: RiskLevel, floor: RiskLevel) -> RiskLevel:
    return current if _ORDER[current] >= _ORDER[floor] else floor


def _raise(current: RiskLevel) -> RiskLevel:
    return _BY_RANK[min(_ORDER[current] + 1, 2)]


def normalize_criticality(value: Any) -> Optional[str]:
    text = str(value).strip().lower() if value is not None else ""
    return text if text in CRITICALITY_LEVELS else None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1")


def profile_from_binding_evidence(evidence: Optional[Mapping[str, Any]]) -> WorkloadRiskProfile:
    """Reads workload labels, annotations and HPA/VPA/PDB facts from GitOps discovery evidence."""
    if not isinstance(evidence, Mapping):
        return WorkloadRiskProfile()
    workload = evidence.get("workload") if isinstance(evidence.get("workload"), Mapping) else {}
    labels = evidence.get("workload_labels") or (workload or {}).get("labels") or {}
    annotations = (workload or {}).get("annotations") or {}
    labels = labels if isinstance(labels, Mapping) else {}
    annotations = annotations if isinstance(annotations, Mapping) else {}

    jvm: Optional[bool] = None
    if JVM_FLAG in labels or JVM_FLAG in annotations:
        jvm = _truthy(labels.get(JVM_FLAG, annotations.get(JVM_FLAG)))
    elif RUNTIME_LABEL in labels or RUNTIME_LABEL in annotations:
        jvm = str(labels.get(RUNTIME_LABEL, annotations.get(RUNTIME_LABEL))).strip().lower() == "jvm"

    hpa_resources: Optional[FrozenSet[str]] = None
    hpas = evidence.get("hpa_targets")
    if evidence.get("hpa_pdb_interactions_checked") is True and isinstance(hpas, list):
        resources: set[str] = set()
        for hpa in hpas:
            if not isinstance(hpa, Mapping):
                continue
            metrics = [m for m in hpa.get("resource_metrics") or () if isinstance(m, Mapping)]
            names = {str(m.get("name") or m.get("resource") or "").lower() for m in metrics} - {""}
            resources.update(names or {"*"})
        hpa_resources = frozenset(resources)

    vpa_mode: Optional[str] = None
    vpas = evidence.get("vpa_targets")
    if evidence.get("vpa_interactions_checked") is True and isinstance(vpas, list):
        modes = [str(v.get("update_mode")) for v in vpas if isinstance(v, Mapping) and v.get("update_mode")]
        conflicting = [mode for mode in modes if mode in CONFLICTING_VPA_UPDATE_MODES]
        vpa_mode = conflicting[0] if conflicting else (modes[0] if modes else "none")

    pdb = evidence.get("pdb_conflict")
    return WorkloadRiskProfile(
        hpa_resources=hpa_resources,
        vpa_update_mode=vpa_mode,
        pdb_conflict=pdb if isinstance(pdb, bool) else None,
        criticality=normalize_criticality(labels.get(CRITICALITY_LABEL)),
        jvm=jvm,
    )


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
    """Risk of a CPU/memory rightsizing change from the pooled usage distribution."""
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
    history: List[str] = []
    if evidence.restart_count:
        if not _is_increase(rec, "memory"):
            return RiskLevel.HIGH, ["restart_history"]
        # Restarts are what a memory increase prevents (E9): recorded, not a risk of the change.
        history = ["restart_history"]
    stats = evidence.memory_usage
    proposed_mem = evidence.proposed.memory_request_bytes
    if stats and proposed_mem:
        if stats.max >= proposed_mem:
            return RiskLevel.HIGH, [*history, "observed_max_exceeds_proposal"]
        if stats.p95 >= proposed_mem * 0.9:
            return RiskLevel.MEDIUM, [*history, "p95_close_to_proposal"]
        return RiskLevel.LOW, [*history, "sufficient_headroom"]
    return RiskLevel.MEDIUM, [*history, "missing_memory_usage"]


def _requests(rec: Any, resource: str) -> tuple:
    if resource == "cpu":
        return rec.current_cpu_request_millicores, rec.recommended_cpu_request_millicores
    return rec.current_memory_request_bytes, rec.recommended_memory_request_bytes


def _is_decrease(rec: Any, resource: str) -> bool:
    current, proposed = _requests(rec, resource)
    return current is not None and proposed is not None and proposed < current


def _is_increase(rec: Any, resource: str) -> bool:
    """A reliability increase of the request (two-way rightsizing, roadmap E9)."""
    current, proposed = _requests(rec, resource)
    return current is not None and proposed is not None and proposed > current


def apply_profile(
    rec: Any,
    level: RiskLevel,
    factors: List[str],
    profile: Optional[WorkloadRiskProfile],
    *,
    requires_restart: bool,
) -> tuple:
    """Folds workload facts into a risk level (risk model v2). Returns ``(level, factors)``."""
    if profile is None:
        return level, factors
    factors = list(factors)

    def add(factor: str) -> None:
        if factor not in factors:
            factors.append(factor)

    if rec.type in _RIGHTSIZING:
        resource = "cpu" if rec.type == RecommendationType.RIGHTSIZING_CPU else "memory"
        proposed = rec.recommended_cpu_request_millicores if resource == "cpu" else rec.recommended_memory_request_bytes
        hottest = profile.replica_cpu_p95_max if resource == "cpu" else profile.replica_memory_p95_max
        if proposed and hottest is not None:
            if hottest >= proposed:
                level = RiskLevel.HIGH
                add("replica_p95_exceeds_proposal")
            elif hottest >= proposed * 0.9:
                level = _at_least(level, RiskLevel.MEDIUM)
                add("replica_p95_close_to_proposal")

        # OOM kills and throttling make a decrease riskier; an increase is the fix
        # for them (E9), so they are kept as factors (the incident it avoids) only.
        increase = _is_increase(rec, resource)
        if resource == "memory" and profile.oom_events:
            if not increase:
                level = RiskLevel.HIGH
            add("oom_history")
        elif resource == "memory" and profile.oom_events is None and _is_decrease(rec, "memory"):
            # Unknown is never healthy: the decrease may land below peaks that OOM-killed it.
            level = _at_least(level, RiskLevel.MEDIUM)
            add(OOM_HISTORY_UNKNOWN)
            if profile.oom_unknown_cause:
                # Which input was missing, for the eligibility message (REC-04).
                add(f"{OOM_UNKNOWN_CAUSE_PREFIX}{profile.oom_unknown_cause}")
        if resource == "cpu" and profile.cpu_throttling_ratio is not None:
            if profile.cpu_throttling_ratio > THROTTLING_HIGH:
                if not increase:
                    level = RiskLevel.HIGH
                add("cpu_throttling_history")
            elif profile.cpu_throttling_ratio > THROTTLING_MEDIUM:
                if not increase:
                    level = _at_least(level, RiskLevel.MEDIUM)
                add("cpu_throttling_history")

        if profile.hpa_resources:
            if resource in profile.hpa_resources:
                level = _at_least(level, RiskLevel.MEDIUM)
                add("hpa_scales_on_resource")
            elif "*" in profile.hpa_resources:
                add("hpa_present")
        if profile.vpa_update_mode in CONFLICTING_VPA_UPDATE_MODES:
            level = RiskLevel.HIGH
            add("vpa_conflicting_writer")
        if profile.jvm and _is_decrease(rec, resource):
            if resource == "memory":
                level = RiskLevel.HIGH
                add("jvm_heap_not_observable")
            else:
                level = _at_least(level, RiskLevel.MEDIUM)
                add("jvm_startup_cpu")
        if resource == "memory" and _is_decrease(rec, "memory"):
            # In-place resize cannot reclaim memory below current usage: best effort or a restart.
            add("memory_decrease_best_effort")
        if resource == "cpu" and profile.static_cpu_manager:
            level = RiskLevel.HIGH
            add("static_cpu_manager_incompatible")

    if profile.pdb_conflict and requires_restart:
        level = _at_least(level, RiskLevel.MEDIUM)
        add("pdb_blocks_disruption")
    if profile.replicas == 1 and requires_restart:
        level = _raise(level)
        add("singleton_workload")
    if profile.criticality == "critical":
        level = _raise(level)
        add("criticality_critical")
    elif profile.criticality == "high":
        level = _at_least(level, RiskLevel.MEDIUM)
        add("criticality_high")
    elif profile.criticality == "low":
        add("criticality_low")
    return level, factors


def assess(
    rec: Recommendation,
    evidence: Optional[RecommendationEvidence],
    config: "Config",
    profile: Optional[WorkloadRiskProfile] = None,
) -> RiskAssessment:
    """Computes risk level, factors, confidence and effort for a recommendation."""
    confidence, confidence_factors = _base_confidence(rec, evidence, config)
    assessment = RiskAssessment(confidence=confidence, confidence_factors=confidence_factors)

    if rec.type in _RIGHTSIZING:
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
        # Deletions cannot be reverted from Git: a deleted pod, namespace, volume or
        # load balancer address is gone (its data, its IP), so never low risk.
        assessment.risk_level, assessment.risk_factors = RiskLevel.MEDIUM, ["no_observed_usage", IRREVERSIBLE_DELETION]
        assessment.effort = EffortLevel.LOW
        assessment.reversible = False
    elif rec.type == RecommendationType.IDLE_NAMESPACE:
        assessment.risk_level, assessment.risk_factors = (
            RiskLevel.HIGH,
            [
                "namespace_decommission",
                IRREVERSIBLE_DELETION,
            ],
        )
        assessment.effort = EffortLevel.MEDIUM
        assessment.reversible = False
    elif rec.type in (RecommendationType.OVERPROVISIONED_NODE, RecommendationType.UNDERUTILIZED_NODE):
        assessment.risk_level, assessment.risk_factors = RiskLevel.HIGH, ["blast_radius_multi_workload"]
        assessment.effort = EffortLevel.HIGH
        assessment.reversible = True
        assessment.requires_restart = True
    elif rec.type == RecommendationType.ORPHANED_PERSISTENT_VOLUME:
        # Deleting a volume deletes its data (reclaim policy Delete) or strands it.
        assessment.risk_level, assessment.risk_factors = RiskLevel.HIGH, ["no_bound_consumer", IRREVERSIBLE_DELETION]
        assessment.effort = EffortLevel.LOW
        assessment.reversible = False
    elif rec.type == RecommendationType.ORPHANED_LOAD_BALANCER:
        # Re-creating the Service rarely gets the same external address back.
        assessment.risk_level, assessment.risk_factors = RiskLevel.MEDIUM, ["no_bound_consumer", IRREVERSIBLE_DELETION]
        assessment.effort = EffortLevel.LOW
        assessment.reversible = False

    assessment.risk_level, assessment.risk_factors = apply_profile(
        rec,
        assessment.risk_level,
        assessment.risk_factors,
        profile,
        requires_restart=assessment.requires_restart,
    )
    if profile is not None and profile.jvm and rec.type == RecommendationType.RIGHTSIZING_MEMORY:
        if _is_decrease(rec, "memory"):
            # The JVM sizes its heap itself: the working set does not show what it needs (E9).
            assessment.confidence = round(assessment.confidence * JVM_MEMORY_DECREASE_CONFIDENCE, 4)
            assessment.confidence_factors = {
                **assessment.confidence_factors,
                "jvm_suspected": JVM_MEMORY_DECREASE_CONFIDENCE,
            }
    return assessment
