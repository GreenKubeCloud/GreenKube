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
    for m in metrics:
        if m.timestamp:
            ts = m.timestamp
            if hourly:
                m.period = ts.strftime("%Y-%m-%dT%H:00")
            elif daily:
                m.period = ts.strftime("%Y-%m-%d")
            elif weekly:
                m.period = ts.strftime("%Y-W%V")  # ISO 8601 week number
            elif monthly:
                m.period = ts.strftime("%Y-%m")
            elif yearly:
                m.period = ts.strftime("%Y")

    groups = defaultdict(list)
    for m in metrics:
        groups[_key_for_metric(m)].append(m)

    result: List[CombinedMetric] = []
    for (namespace, pod, period), items in groups.items():
        total_joules = sum(i.joules for i in items)
        total_co2 = sum(i.co2e_grams for i in items)
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

        combined = CombinedMetric(
            pod_name=pod,
            namespace=namespace,
            period=(None if period == "__now__" else period),
            total_cost=total_cost,
            co2e_grams=total_co2,
            pue=weighted_pue,
            grid_intensity=weighted_grid,
            joules=total_joules,
            cpu_request=cpu_request,
            memory_request=memory_request,
            timestamp=first_timestamp,
            duration_seconds=total_duration if total_duration > 0 else None,
            # grid_intensity_timestamp is lost in aggregation, which is expected.
        )
        result.append(combined)

    return result
