"""Idle namespace detection.

Decommissioning deletes everything in a namespace, including what GreenKube
cannot measure (custom resources, Secrets, ConfigMaps, external consumers),
so an idle-namespace recommendation is a review prompt with evidence, and it
is only emitted when GreenKube could read the namespace (fail closed). It is
never emitted for namespaces Kubernetes or the platform need (``kube-*``,
``*-system``, monitoring, GitOps, ingress, storage...; whatever
``RECOMMEND_SYSTEM_NAMESPACES`` says), opted-out or terminating namespaces,
namespaces younger than ``DELETION_MIN_UNUSED_DAYS``, namespaces that host an
operator, controller, webhook or DaemonSet, or when the low-energy condition
covers less than ``DELETION_MIN_UNUSED_DAYS`` of observations.
"""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List

from greenkube.core.deletion_safety import (
    NODE_AGENT_KINDS,
    UNDELETABLE_NAMESPACES,
    format_days,
    is_infrastructure_namespace,
    looks_event_driven,
    min_unused_period,
    opt_out_reason,
)
from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.utils.time import ensure_utc

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext

#: Kept for compatibility: the names every release excluded (now a subset of the infrastructure rule).
SYSTEM_NAMESPACES = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "coredns",
    "istio-system",
    "kubernetes-dashboard",
}


class IdleNamespaceAnalyzer(Analyzer):
    """Identifies namespaces with minimal total activity, with evidence."""

    capability = "namespace_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        namespaces = context.namespace_info
        if namespaces is None:
            # Namespaces could not be read: opt-outs and ages are unknown.
            return recs
        now = ensure_utc(context.window_end or datetime.now(timezone.utc))
        minimum = min_unused_period(cfg)

        targets_by_ns: dict = {}
        for (ns, kind, name), _summary in context.workload_summaries():
            targets_by_ns.setdefault(ns, []).append((kind, name))

        for ns, summaries in context.namespace_targets().items():
            if ns in SYSTEM_NAMESPACES or ns in UNDELETABLE_NAMESPACES or is_infrastructure_namespace(ns):
                continue
            joules = sum(summary.joules for summary in summaries)
            cost = sum(summary.total_cost for summary in summaries)
            if not (joules < cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD and cost > 0):
                continue
            info = namespaces.get(ns)
            if info is None or str(getattr(info, "phase", "Active")) != "Active":
                continue
            if opt_out_reason(info.metadata):
                continue
            created_at = getattr(info, "created_at", None)
            if created_at is not None and now - ensure_utc(created_at) < minimum:
                continue
            targets = targets_by_ns.get(ns, [])
            if any(kind in NODE_AGENT_KINDS or looks_event_driven(name) for kind, name in targets):
                continue
            first_hours = [s.first_hour for s in summaries if s.first_hour is not None]
            last_hours = [s.last_hour for s in summaries if s.last_hour is not None]
            if not first_hours or not last_hours:
                continue
            span = max(last_hours) - min(first_hours) + timedelta(hours=1)
            if span < minimum:
                continue

            # Decommissioning deletes every workload of the namespace.
            projector = ImpactProjector.for_context(context)
            rows = [row for row in context.metrics if row.namespace == ns and not row.container_name]
            delta = projector.workload_deletion(projector.workload_ref(ns, "Namespace", ns), rows)
            evidence = [
                f"{len(targets)} workload(s) consumed {joules:.0f} J in total "
                f"(threshold {cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD:.0f} J) over {format_days(span)}",
                "no operator, controller, webhook or DaemonSet runs in it",
                "not an infrastructure namespace and not opted out",
            ]
            gitops = info.gitops_manager if hasattr(info, "gitops_manager") else None
            caveat = ""
            if gitops:
                evidence.append(f"managed by {gitops}")
                caveat = " It is managed by GitOps: remove it from Git, a direct deletion would be re-created."
            recs.append(
                Recommendation(
                    namespace=ns,
                    type=RecommendationType.IDLE_NAMESPACE,
                    scope="namespace",
                    description=(
                        f"Namespace '{ns}' appears idle: it consumed only {joules:.0f}J over "
                        f"{format_days(span)} but costs ${cost:.4f}. Review it before decommissioning: "
                        f"GreenKube does not see its custom resources, Secrets or external consumers.{caveat}"
                    ),
                    reason=(
                        f"Evidence that it is unused: {'; '.join(evidence)}. Annotate the namespace with "
                        f'greenkube.io/ignore="true" to keep it.'
                    ),
                    priority="low",
                    **impact_fields(delta),
                )
            )
        return recs
