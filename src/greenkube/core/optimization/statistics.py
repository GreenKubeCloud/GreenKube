# src/greenkube/core/optimization/statistics.py
"""Statistical helpers shared by recommendation analyzers."""

import math
from typing import TYPE_CHECKING, List, Optional, Tuple

from greenkube.models.metrics import CombinedMetric

if TYPE_CHECKING:
    from greenkube.core.config import Config

SECONDS_PER_YEAR = 365 * 24 * 60 * 60


def percentile(values: List[float], p: float) -> float:
    """Computes the p-th percentile of a list of values (0-100 scale)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def usage_stats(
    series: List[CombinedMetric],
    usage_attr: str,
    max_attr: str,
) -> Tuple[float, float, List[float]]:
    """Returns weighted average, observed maximum, and average points for a usage series."""
    weighted_total = 0.0
    sample_total = 0
    average_points: List[float] = []
    observed_max_values: List[float] = []

    for metric in series:
        usage = getattr(metric, usage_attr)
        if usage is None:
            continue

        sample_count = max(int(getattr(metric, "sample_count", 1) or 1), 1)
        usage_float = float(usage)
        weighted_total += usage_float * sample_count
        sample_total += sample_count
        average_points.append(usage_float)

        max_usage = getattr(metric, max_attr, None)
        observed_max_values.append(float(max_usage if max_usage is not None else usage_float))

    if sample_total == 0 or not observed_max_values:
        return 0.0, 0.0, []

    return weighted_total / sample_total, max(observed_max_values), average_points


def latest_request_value(series: List[CombinedMetric], request_attr: str) -> int:
    """Returns the latest observed resource request, falling back to the maximum when timestamps are absent."""
    timestamped = [metric for metric in series if metric.timestamp is not None]
    if not timestamped:
        return max((getattr(metric, request_attr) or 0) for metric in series)

    latest_timestamp = max(metric.timestamp for metric in timestamped if metric.timestamp is not None)
    latest_metrics = [metric for metric in timestamped if metric.timestamp == latest_timestamp]
    return max((getattr(metric, request_attr) or 0) for metric in latest_metrics)


def balanced_rightsizing_target(config: "Config", avg_usage: float, observed_max: float, p95_usage: float) -> int:
    """Calculates a rightsizing target that balances steady-state and peak demand."""
    balanced_peak = (avg_usage + observed_max) / 2.0
    return max(int(max(p95_usage, balanced_peak) * config.RIGHTSIZING_HEADROOM), 1)


def annualized_window_total(total_value: float, analysis_window_seconds: Optional[float]) -> float:
    """Project a measured window total to a yearly total when a window is known."""
    if analysis_window_seconds is None or analysis_window_seconds <= 0:
        return total_value
    return total_value * (SECONDS_PER_YEAR / analysis_window_seconds)


def resource_savings_ratio(current_value: int, recommended_value: int) -> float | None:
    """Returns the proportional request reduction, or None if no reduction exists."""
    if current_value <= 0 or recommended_value >= current_value:
        return None
    return 1.0 - (recommended_value / current_value)
