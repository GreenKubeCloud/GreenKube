# src/greenkube/core/aggregator.py
"""
Aggregates CombinedMetric data by (namespace, pod, period).
This is useful for grouping metrics over time intervals.
"""

from collections import defaultdict
from typing import Iterable, List, Tuple

from ..models.metrics import CombinedMetric


def _key_for_metric(metric: CombinedMetric) -> Tuple[str, str, str]:
    """Return a grouping key (namespace,pod,period).

    Period may be None for 'now' reports; use an explicit marker in that
    case so aggregation still groups by pod for a single period.
    """
    period = metric.period or "__now__"
    return (metric.namespace, metric.pod_name, period)


def aggregate_metrics(
    metrics: Iterable[CombinedMetric],
    hourly: bool = False,
    daily: bool = False,
    weekly: bool = False,
    monthly: bool = False,
    yearly: bool = False,
) -> List[CombinedMetric]:
    """Aggregate a sequence of CombinedMetric into one per (namespace,pod,period).

    Aggregation rules:
    - Joules (energy) are summed.
    - CO2e grams are summed.
    - total_cost is summed.
    - grid_intensity and pue are averaged (simple arithmetic mean) weighted by time if available.
      Since CombinedMetric doesn't include duration, we weight by joules when present: the
      idea is that higher energy samples should contribute more to the average intensity.
    - cpu_request and memory_request keep the max value observed (requests are capacities).

    Returns a new list of CombinedMetric objects.
    """
    # First, assign the correct period string to each metric based on its
    # timestamp and the requested grouping.
    # We work on copies to avoid mutating the caller's input objects.
    metrics_list = []
    for m in metrics:
        mc = m.model_copy()
        if mc.timestamp:
            ts = mc.timestamp
            if hourly:
                mc.period = ts.strftime("%Y-%m-%dT%H:00")
            elif daily:
                mc.period = ts.strftime("%Y-%m-%d")
            elif weekly:
                mc.period = ts.strftime("%Y-W%V")  # ISO 8601 week number
            elif monthly:
                mc.period = ts.strftime("%Y-%m")
            elif yearly:
                mc.period = ts.strftime("%Y")
        metrics_list.append(mc)

    groups = defaultdict(list)
    for m in metrics_list:
        groups[_key_for_metric(m)].append(m)

    result: List[CombinedMetric] = []
    for (namespace, pod, period), items in groups.items():
        total_joules = sum(i.joules for i in items)
        total_co2 = sum(i.co2e_grams for i in items)
        total_embodied_co2 = sum(i.embodied_co2e_grams for i in items)
        total_cost = sum(i.total_cost for i in items)
        total_duration = sum(i.duration_seconds for i in items if i.duration_seconds is not None)

        # For grid_intensity and pue, compute weighted average by duration when possible
        if total_duration > 0:
            weighted_grid = sum((i.grid_intensity or 0.0) * (i.duration_seconds or 0) for i in items) / total_duration
            weighted_pue = sum((i.pue or 1.0) * (i.duration_seconds or 0) for i in items) / total_duration
        else:
            # Fallback to simple mean if no duration
            weighted_grid = sum(i.grid_intensity or 0.0 for i in items) / len(items) if items else 0.0
            weighted_pue = sum((i.pue or 1.0) for i in items) / len(items)

        cpu_request = max((i.cpu_request or 0) for i in items)
        memory_request = max((i.memory_request or 0) for i in items)
        # Take the timestamp of the first item in the group for the aggregated metric
        first_timestamp = items[0].timestamp if items else None

        # Preserve metadata through aggregation
        # is_estimated is True if ANY item was estimated
        agg_is_estimated = any(i.is_estimated for i in items)
        # Collect unique estimation reasons
        agg_reasons = list({r for i in items for r in (i.estimation_reasons or [])})
        # Use the most common node, emaps_zone, and instance type
        agg_node = (
            max(
                {i.node for i in items if i.node},
                key=lambda n: sum(1 for i in items if i.node == n),
                default=None,
            )
            if any(i.node for i in items)
            else None
        )
        agg_emaps_zone = (
            max(
                {i.emaps_zone for i in items if i.emaps_zone},
                key=lambda z: sum(1 for i in items if i.emaps_zone == z),
                default=None,
            )
            if any(i.emaps_zone for i in items)
            else None
        )
        agg_instance_type = (
            max(
                {i.node_instance_type for i in items if i.node_instance_type},
                key=lambda t: sum(1 for i in items if i.node_instance_type == t),
                default=None,
            )
            if any(i.node_instance_type for i in items)
            else None
        )
        agg_node_zone = (
            max(
                {i.node_zone for i in items if i.node_zone},
                key=lambda z: sum(1 for i in items if i.node_zone == z),
                default=None,
            )
            if any(i.node_zone for i in items)
            else None
        )
        # Average usage metrics
        cpu_usages = [i.cpu_usage_millicores for i in items if i.cpu_usage_millicores is not None]
        agg_cpu_usage = int(round(sum(cpu_usages) / len(cpu_usages))) if cpu_usages else None
        mem_usages = [i.memory_usage_bytes for i in items if i.memory_usage_bytes is not None]
        agg_mem_usage = int(round(sum(mem_usages) / len(mem_usages))) if mem_usages else None
        # Use the latest calculation_version
        agg_calc_version = items[-1].calculation_version if items else None

        combined = CombinedMetric(
            pod_name=pod,
            namespace=namespace,
            period=(None if period == "__now__" else period),
            total_cost=total_cost,
            co2e_grams=total_co2,
            embodied_co2e_grams=total_embodied_co2,
            pue=weighted_pue,
            grid_intensity=weighted_grid,
            joules=total_joules,
            cpu_request=cpu_request,
            memory_request=memory_request,
            cpu_usage_millicores=agg_cpu_usage,
            memory_usage_bytes=agg_mem_usage,
            timestamp=first_timestamp,
            duration_seconds=total_duration if total_duration > 0 else None,
            node=agg_node,
            node_instance_type=agg_instance_type,
            node_zone=agg_node_zone,
            emaps_zone=agg_emaps_zone,
            is_estimated=agg_is_estimated,
            estimation_reasons=agg_reasons,
            calculation_version=agg_calc_version,
        )
        result.append(combined)

    return result
