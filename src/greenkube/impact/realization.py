# src/greenkube/impact/realization.py
"""``RealizationContext``: how much released capacity becomes removed node-hours (roadmap B6).

Request cuts lower a workload's *allocated* cost and CO2e at once, but the
bill only shrinks when nodes go away. The realization factor estimates that
share per node pool from what the cluster can actually do:

=====================================================================  ======
Situation                                                               Factor
=====================================================================  ======
No autoscaler (fixed nodes: allocated savings only)                     0.0
Karpenter ``WhenEmptyOrUnderutilized``/``WhenUnderutilized``/``Balanced`` 0.8
Karpenter ``WhenEmpty`` (nodes are removed only once empty)             0.3
Cluster Autoscaler                                                      0.6
Karpenter disruption budget of zero nodes, always active                0.0
Any of the above with blockers                                          × 0.5
=====================================================================  ======

Blockers are ``karpenter.sh/do-not-disrupt`` (or the legacy
``karpenter.sh/do-not-evict``) and ``cluster-autoscaler.kubernetes.io/safe-to-evict: "false"``
on pods, ``karpenter.sh/do-not-disrupt`` and
``cluster-autoscaler.kubernetes.io/scale-down-disabled`` on nodes,
PodDisruptionBudgets that allow no disruption, scheduled zero budgets and an
unhealthy Cluster Autoscaler.

Everything here is pure except :func:`refresh_realization_context`, which
reads the cluster (through :class:`~greenkube.collectors.autoscaler_collector.AutoscalerCollector`)
from the controller's collection cycle and caches the result in the process.
Projections read the cache synchronously through :func:`current_realization_context`,
so analyzers and connectors need no change. ``IMPACT_REALIZATION_FACTOR``, when
set, overrides the context for every projection.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

logger = logging.getLogger(__name__)

FACTOR_NO_AUTOSCALER = 0.0
FACTOR_KARPENTER_CONSOLIDATING = 0.8
FACTOR_KARPENTER_WHEN_EMPTY = 0.3
FACTOR_CLUSTER_AUTOSCALER = 0.6
BLOCKER_MULTIPLIER = 0.5

#: Karpenter consolidation policies that consolidate under-utilized nodes.
CONSOLIDATING_POLICIES = frozenset({"WhenEmptyOrUnderutilized", "WhenUnderutilized", "Balanced"})

#: Realization sources recorded as ``assumptions.realization_factor_source``.
SOURCE_CONTEXT = "realization_context"
SOURCE_UNOBSERVED = "realization_context:unobserved"
SOURCE_CONFIG = "config:IMPACT_REALIZATION_FACTOR"

#: Factor used until the controller has observed the cluster once (no change from v1).
UNOBSERVED_FACTOR = 1.0

#: How long an observed context is reused before the cluster is read again.
REFRESH_INTERVAL = timedelta(minutes=10)

KARPENTER_POOL_LABELS = ("karpenter.sh/nodepool", "karpenter.sh/provisioner-name")
POD_BLOCKING_ANNOTATIONS = {
    "karpenter.sh/do-not-disrupt": "true",
    "karpenter.sh/do-not-evict": "true",
    "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
}
NODE_BLOCKING_ANNOTATIONS = {
    "karpenter.sh/do-not-disrupt": "true",
    "cluster-autoscaler.kubernetes.io/scale-down-disabled": "true",
}
#: Pool of nodes managed by the Cluster Autoscaler (or by nothing).
CLUSTER_POOL = "__cluster__"


# --------------------------------------------------------------------------- #
# Observations (plain values filled by the collector)                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NodeObservation:
    name: str
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PodObservation:
    namespace: str
    name: str
    node: Optional[str]
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PdbObservation:
    namespace: str
    name: str
    match_labels: Mapping[str, str] = field(default_factory=dict)
    #: ``(key, operator, values)`` of ``matchExpressions``.
    match_expressions: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = ()
    disruptions_allowed: Optional[int] = None
    expected_pods: Optional[int] = None

    @property
    def blocks(self) -> bool:
        """True when the budget currently allows no voluntary disruption of a non-empty selection."""
        return self.disruptions_allowed == 0 and (self.expected_pods or 0) > 0

    def selects(self, pod: PodObservation) -> bool:
        if pod.namespace != self.namespace:
            return False
        if not self.match_labels and not self.match_expressions:
            return True
        for key, value in self.match_labels.items():
            if pod.labels.get(key) != value:
                return False
        for key, operator, values in self.match_expressions:
            present = key in pod.labels
            value = pod.labels.get(key)
            if operator == "In" and value not in values:
                return False
            if operator == "NotIn" and present and value in values:
                return False
            if operator == "Exists" and not present:
                return False
            if operator == "DoesNotExist" and present:
                return False
        return True


@dataclass(frozen=True)
class KarpenterPoolObservation:
    name: str
    consolidation_policy: Optional[str] = None
    #: ``spec.disruption.budgets`` as returned by the API.
    budgets: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class ClusterAutoscalerObservation:
    """The ``kube-system/cluster-autoscaler-status`` ConfigMap (loosely structured text or YAML)."""

    status_text: str = ""

    @property
    def health(self) -> Optional[str]:
        """``Healthy``/``Unhealthy`` from the cluster-wide section, when it can be parsed."""
        match = re.search(r"(?im)^\s*health\s*:\s*(?:status\s*:\s*)?([A-Za-z]+)", self.status_text or "")
        if match is None:
            # YAML status (Cluster Autoscaler >= 1.30): clusterWide.health.status.
            match = re.search(r"(?is)health\s*:\s*\n\s*status\s*:\s*([A-Za-z]+)", self.status_text or "")
        return match.group(1) if match else None


@dataclass(frozen=True)
class RealizationInputs:
    """Everything read from the cluster; ``None`` members could not be read (RBAC, API errors)."""

    nodes: Sequence[NodeObservation] = ()
    pods: Optional[Sequence[PodObservation]] = ()
    pdbs: Optional[Sequence[PdbObservation]] = ()
    karpenter_pools: Optional[Sequence[KarpenterPoolObservation]] = ()
    cluster_autoscaler: Optional[ClusterAutoscalerObservation] = None
    #: Inputs that could not be read; reported as reasons, never silently ignored.
    unavailable: Tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Context                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PoolRealization:
    """Realization factor of one node pool and why."""

    pool: str
    factor: float
    autoscaler: str
    reasons: Tuple[str, ...]
    nodes: Tuple[str, ...] = ()
    consolidation_policy: Optional[str] = None
    blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pool": self.pool,
            "factor": self.factor,
            "autoscaler": self.autoscaler,
            "consolidation_policy": self.consolidation_policy,
            "blocked": self.blocked,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RealizationContext:
    """Realization factors per node pool, and the node → pool mapping."""

    pools: Mapping[str, PoolRealization]
    node_pools: Mapping[str, str]
    #: Pool applied to nodes the context does not know (new nodes, unresolved samples).
    default_pool: str
    observed_at: datetime
    notes: Tuple[str, ...] = ()

    def pool_for(self, node: Optional[str]) -> PoolRealization:
        name = self.node_pools.get(node or "", self.default_pool)
        return self.pools.get(name) or self.pools[self.default_pool]

    def factor_for(self, node: Optional[str]) -> float:
        return self.pool_for(node).factor


def _budget_blocks(budget: Mapping[str, Any]) -> Optional[str]:
    """``always`` when a budget forbids consolidation at all times, ``scheduled`` when sometimes."""
    nodes = str(budget.get("nodes", "")).strip()
    if nodes not in ("0", "0%"):
        return None
    reasons = budget.get("reasons") or []
    if reasons and not any(str(reason) in ("Underutilized", "Empty") for reason in reasons):
        return None
    return "scheduled" if budget.get("schedule") else "always"


def _pod_blocks(pod: PodObservation) -> Optional[str]:
    for key, value in POD_BLOCKING_ANNOTATIONS.items():
        if str(pod.annotations.get(key, "")).strip().lower() == value:
            return f"pod {pod.namespace}/{pod.name} has {key}={value}"
    return None


def _node_blocks(node: NodeObservation) -> Optional[str]:
    for key, value in NODE_BLOCKING_ANNOTATIONS.items():
        if str(node.annotations.get(key, "")).strip().lower() == value:
            return f"node {node.name} has {key}={value}"
    return None


def _karpenter_pool_of(node: NodeObservation, known: Iterable[str]) -> Optional[str]:
    names = set(known)
    for label in KARPENTER_POOL_LABELS:
        value = node.labels.get(label)
        if value and (value in names or not names):
            return value
    return None


def resolve_realization(inputs: RealizationInputs, now: Optional[datetime] = None) -> RealizationContext:
    """Computes the per-pool realization factors (pure)."""
    now = now or datetime.now(timezone.utc)
    karpenter = {pool.name: pool for pool in inputs.karpenter_pools or ()}
    node_pools: Dict[str, str] = {}
    members: Dict[str, List[str]] = {}
    for node in inputs.nodes:
        pool = _karpenter_pool_of(node, karpenter) or CLUSTER_POOL
        node_pools[node.name] = pool
        members.setdefault(pool, []).append(node.name)

    # Blockers per pool, with their first few explanations.
    blockers: Dict[str, List[str]] = {}

    def _block(pool: str, reason: str) -> None:
        blockers.setdefault(pool, []).append(reason)

    for node in inputs.nodes:
        reason = _node_blocks(node)
        if reason:
            _block(node_pools[node.name], reason)
    pods = list(inputs.pods or ())
    for pod in pods:
        reason = _pod_blocks(pod)
        if reason and pod.node:
            _block(node_pools.get(pod.node, CLUSTER_POOL), reason)
    for pdb in inputs.pdbs or ():
        if not pdb.blocks:
            continue
        pools_hit = {node_pools.get(pod.node, CLUSTER_POOL) for pod in pods if pod.node and pdb.selects(pod)}
        for pool in pools_hit:
            _block(pool, f"PodDisruptionBudget {pdb.namespace}/{pdb.name} allows no disruption")

    notes = tuple(f"{name} could not be read" for name in inputs.unavailable)
    ca = inputs.cluster_autoscaler
    pools: Dict[str, PoolRealization] = {}

    for name, pool in karpenter.items():
        policy = pool.consolidation_policy
        reasons: List[str] = []
        if policy in CONSOLIDATING_POLICIES:
            factor = FACTOR_KARPENTER_CONSOLIDATING
            reasons.append(f"Karpenter NodePool {name} consolidates with {policy}")
        elif policy == "WhenEmpty":
            factor = FACTOR_KARPENTER_WHEN_EMPTY
            reasons.append(f"Karpenter NodePool {name} removes nodes only when empty (WhenEmpty)")
        else:
            factor = FACTOR_KARPENTER_WHEN_EMPTY
            reasons.append(f"Karpenter NodePool {name} has an unknown consolidation policy {policy!r}")
        budget_states = {_budget_blocks(budget) for budget in pool.budgets}
        blocked = False
        if "always" in budget_states:
            factor = 0.0
            reasons.append(f"Karpenter NodePool {name} has a disruption budget of zero nodes")
        else:
            pool_blockers = list(blockers.get(name, []))
            if "scheduled" in budget_states:
                pool_blockers.append(f"Karpenter NodePool {name} has a scheduled zero disruption budget")
            if pool_blockers:
                blocked = True
                factor *= BLOCKER_MULTIPLIER
                reasons.extend(_summarize(pool_blockers))
        pools[name] = PoolRealization(
            pool=name,
            factor=factor,
            autoscaler="karpenter",
            reasons=tuple(reasons),
            nodes=tuple(sorted(members.get(name, []))),
            consolidation_policy=policy,
            blocked=blocked,
        )

    # Nodes outside Karpenter pools: Cluster Autoscaler, or fixed nodes.
    reasons = []
    blocked = False
    if ca is not None:
        factor = FACTOR_CLUSTER_AUTOSCALER
        autoscaler = "cluster_autoscaler"
        reasons.append("Cluster Autoscaler is running (kube-system/cluster-autoscaler-status)")
        cluster_blockers = list(blockers.get(CLUSTER_POOL, []))
        health = ca.health
        if health and health.lower() != "healthy":
            cluster_blockers.append(f"Cluster Autoscaler reports {health}")
        if cluster_blockers:
            blocked = True
            factor *= BLOCKER_MULTIPLIER
            reasons.extend(_summarize(cluster_blockers))
    else:
        factor = FACTOR_NO_AUTOSCALER
        autoscaler = "none"
        if inputs.karpenter_pools:
            reasons.append("Node is not managed by a Karpenter NodePool and no Cluster Autoscaler runs")
        else:
            reasons.append("No Cluster Autoscaler or Karpenter: fixed nodes, savings stay allocated, not billed")
    pools[CLUSTER_POOL] = PoolRealization(
        pool=CLUSTER_POOL,
        factor=factor,
        autoscaler=autoscaler,
        reasons=tuple(reasons),
        nodes=tuple(sorted(members.get(CLUSTER_POOL, []))),
        blocked=blocked,
    )
    return RealizationContext(
        pools=pools,
        node_pools=node_pools,
        default_pool=CLUSTER_POOL,
        observed_at=now,
        notes=notes,
    )


def _summarize(reasons: Sequence[str], limit: int = 3) -> List[str]:
    unique = list(dict.fromkeys(reasons))
    if len(unique) <= limit:
        return [f"blocker: {reason}" for reason in unique]
    return [f"blocker: {reason}" for reason in unique[:limit]] + [f"blocker: {len(unique) - limit} more"]


# --------------------------------------------------------------------------- #
# Projection-side view                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RealizationResolution:
    """Realization of one projection: the factor, its source and why."""

    factor: float
    source: str
    reasons: Tuple[str, ...] = ()
    pools: Tuple[Dict[str, Any], ...] = ()


def realization_for_nodes(context: RealizationContext, weights: Mapping[Optional[str], float]) -> RealizationResolution:
    """Hours-weighted realization factor over the nodes a workload ran on."""
    total = sum(max(weight, 0.0) for weight in weights.values())
    used: Dict[str, PoolRealization] = {}
    if total <= 0:
        pool = context.pools[context.default_pool]
        used[pool.pool] = pool
        factor = pool.factor
    else:
        factor = 0.0
        for node, weight in weights.items():
            pool = context.pool_for(node)
            used[pool.pool] = pool
            factor += pool.factor * max(weight, 0.0) / total
    reasons: List[str] = []
    for pool in used.values():
        reasons.extend(pool.reasons)
    reasons.extend(context.notes)
    return RealizationResolution(
        factor=min(max(factor, 0.0), 1.0),
        source=SOURCE_CONTEXT,
        reasons=tuple(dict.fromkeys(reasons)),
        pools=tuple(pool.to_dict() for pool in used.values()),
    )


# --------------------------------------------------------------------------- #
# Process cache, refreshed by the controller                                  #
# --------------------------------------------------------------------------- #


class RealizationCollector(Protocol):
    async def collect(self) -> Optional[RealizationInputs]: ...


@dataclass
class _State:
    context: Optional[RealizationContext] = None
    refreshed_at: Optional[datetime] = None


_STATE = _State()


def current_realization_context() -> Optional[RealizationContext]:
    """The last observed context of this process, or None before the first observation."""
    return _STATE.context


def set_realization_context(context: Optional[RealizationContext]) -> None:
    """Replaces the cached context (tests and one-shot commands)."""
    _STATE.context = context
    _STATE.refreshed_at = context.observed_at if context is not None else None


def realization_override(config: Any) -> Optional[float]:
    """``IMPACT_REALIZATION_FACTOR`` when it is set to a number in [0, 1]."""
    value = getattr(config, "IMPACT_REALIZATION_FACTOR", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(max(float(value), 0.0), 1.0)


async def refresh_realization_context(
    config: Any,
    *,
    collector: Optional[RealizationCollector] = None,
    now: Optional[datetime] = None,
    force: bool = False,
) -> Optional[RealizationContext]:
    """Reads the cluster and caches a fresh context, at most every :data:`REFRESH_INTERVAL`.

    Runs only inside a cluster (``KUBERNETES_SERVICE_HOST``) unless a
    collector is given, and never when ``IMPACT_REALIZATION_FACTOR`` pins the
    factor. Failures keep the previous context.
    """
    if realization_override(config) is not None:
        return _STATE.context
    if collector is None and not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return _STATE.context
    now = now or datetime.now(timezone.utc)
    if not force and _STATE.refreshed_at is not None and now - _STATE.refreshed_at < REFRESH_INTERVAL:
        return _STATE.context
    if collector is None:
        from greenkube.collectors.autoscaler_collector import AutoscalerCollector

        collector = AutoscalerCollector()
    # Mark the attempt first: one slow or failing read is not retried by every caller.
    _STATE.refreshed_at = now
    try:
        inputs = await collector.collect()
    except Exception as error:  # pragma: no cover - collectors degrade on their own
        logger.warning("Could not read the realization context: %s", error)
        inputs = None
    if inputs is None:
        return _STATE.context
    context = resolve_realization(inputs, now)
    _STATE.context = context
    logger.info(
        "Realization context: %s",
        ", ".join(f"{name}={pool.factor:g}" for name, pool in sorted(context.pools.items())),
    )
    return context
