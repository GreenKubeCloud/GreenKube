# src/greenkube/core/optimization/analyzers/off_peak.py
"""Off-peak scaling: workloads active only during certain hours."""

from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


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
    """Identifies workloads active only during certain hours."""

    capability = "off_peak"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config

        for (ns, target_kind, target_name), series in context.target_series().items():
            timed = [
                (m.timestamp, m.cpu_usage_millicores)
                for m in series
                if m.timestamp is not None and m.cpu_usage_millicores is not None
            ]
            if len(timed) < 6:
                continue

            hourly_usage: Dict[int, List[float]] = defaultdict(list)
            for ts, usage in timed:
                hourly_usage[ts.hour].append(float(usage))

            if not hourly_usage:
                continue

            hourly_avg = {h: sum(v) / len(v) for h, v in hourly_usage.items()}
            peak_usage = max(hourly_avg.values()) if hourly_avg else 0
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
                    )
                )
        return recs
