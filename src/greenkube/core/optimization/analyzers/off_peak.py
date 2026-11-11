"""Off-peak scaling: workloads active only during certain hours."""

from typing import TYPE_CHECKING, List

from greenkube.core.deletion_safety import (
    NON_SCALABLE_KINDS,
    is_infrastructure_namespace,
    looks_event_driven,
    opt_out_reason,
)
from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext

#: Fewer timestamped CPU samples than this give no meaningful daily profile.
MIN_PROFILE_SAMPLES = 6


def find_longest_consecutive_hours(hours: List[int]) -> List[int]:
    """Finds the longest run of consecutive hours (wrapping around midnight)."""
    if not hours:
        return []

    hours_set = set(hours)
    best: List[int] = []

    for start in hours:
        run = []
        h = start
        while h in hours_set:
            run.append(h)
            h = (h + 1) % 24
            if h not in hours_set or h == start:
                break
        if len(run) > len(best):
            best = run

    return best


class OffPeakAnalyzer(Analyzer):
    """Identifies workloads active only during certain hours.

    Reads the pod-level average CPU per UTC hour of day of each target, from
    the usage snapshot (roadmap E3).

    Scaling to zero stops a workload, so it is never proposed for workloads
    that cannot be scaled (DaemonSets, bare pods, Jobs, CronJobs), workloads
    an HPA owns (its minReplicas would fight the schedule), operators,
    controllers and webhooks (they stop reconciling), infrastructure
    namespaces, or namespaces opted out with ``greenkube.io/ignore``.
    """

    capability = "off_peak"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        snapshot = context.usage_snapshot()
        hpa_targets = context.hpa_targets or set()

        for target, _summary in context.workload_summaries():
            ns, target_kind, target_name = target
            if self._unsafe_to_stop(context, target, hpa_targets):
                continue
            profile = snapshot.hour_profiles.get(target)
            if not profile or snapshot.hour_profile_samples.get(target, 0) < MIN_PROFILE_SAMPLES:
                continue

            hourly_avg = {hour: value for hour, value in enumerate(profile) if value is not None}
            if not hourly_avg:
                continue

            peak_usage = max(hourly_avg.values())
            if peak_usage == 0:
                continue

            idle_threshold = peak_usage * cfg.OFF_PEAK_IDLE_THRESHOLD

            idle_hours = sorted([h for h, avg in hourly_avg.items() if avg < idle_threshold])
            consecutive = find_longest_consecutive_hours(idle_hours)

            if len(consecutive) >= cfg.OFF_PEAK_MIN_IDLE_HOURS:
                start_h = consecutive[0]
                end_h = (consecutive[-1] + 1) % 24
                cron_schedule = f"Scale to 0: {start_h:02d}:00-{end_h:02d}:00 UTC"
                label = target_label(target_kind, target_name)
                # Replica-hours removed during the idle hours, priced at the
                # workload's allocated rate (Impact Model replica/schedule rule).
                projector = ImpactProjector.for_context(context)
                delta = projector.replica_schedule(
                    projector.workload_ref(ns, target_kind, target_name),
                    context.workload_rows(target),
                    consecutive,
                )

                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.OFF_PEAK_SCALING,
                        scope=scope_for_target_kind(target_kind),
                        **owner_fields(target_kind, target_name),
                        description=(
                            f"{label} is idle {len(consecutive)}h/day "
                            f"({start_h:02d}:00-{end_h:02d}:00 UTC). "
                            f"Consider scaling to zero during off-peak hours."
                        ),
                        reason=(
                            f"Usage drops below {cfg.OFF_PEAK_IDLE_THRESHOLD:.0%} of peak "
                            f"for {len(consecutive)} consecutive hours."
                        ),
                        priority="medium",
                        cron_schedule=cron_schedule,
                        **impact_fields(delta),
                    )
                )
        return recs

    @staticmethod
    def _unsafe_to_stop(context: "OptimizationContext", target, hpa_targets) -> bool:
        ns, target_kind, target_name = target
        if target_kind in NON_SCALABLE_KINDS or target in hpa_targets:
            return True
        if is_infrastructure_namespace(ns) or looks_event_driven(target_name):
            return True
        info = (context.namespace_info or {}).get(ns)
        return info is not None and opt_out_reason(getattr(info, "metadata", None)) is not None
