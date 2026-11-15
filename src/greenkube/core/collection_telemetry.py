"""Low-cardinality telemetry for the metric collection pipeline (roadmap A2, A6, A10).

Metrics are registered on the control-loop registry of
:mod:`greenkube.core.observability`, so every process that exposes that
registry (API today, controller once it serves ``/metrics``) publishes them.
"""

from __future__ import annotations

from datetime import datetime

from prometheus_client import Counter, Gauge, Histogram

from .observability import REGISTRY

DROP_REASON_NON_FINITE = "non_finite"
DROP_REASON_NEGATIVE = "negative_clamped"
DROP_REASON_MALFORMED = "malformed"
DROP_REASON_OUT_OF_WINDOW = "out_of_window"
_DROP_REASONS = frozenset(
    {DROP_REASON_NON_FINITE, DROP_REASON_NEGATIVE, DROP_REASON_MALFORMED, DROP_REASON_OUT_OF_WINDOW}
)
_ERROR_REASONS = frozenset({"collection_failed", "leadership_lost"})

WATERMARK_LAG = Gauge(
    "greenkube_collection_watermark_lag_seconds",
    "Seconds between now and the end of the last fully collected window, per source.",
    ["source"],
    registry=REGISTRY,
)
GAP_WINDOWS = Counter(
    "greenkube_collection_gap_windows_total",
    "Windows abandoned because they were older than COLLECTION_MAX_BACKFILL.",
    ["source"],
    registry=REGISTRY,
)
WINDOWS_COLLECTED = Counter(
    "greenkube_collection_windows_total",
    "Collection chunks written and committed, per source.",
    ["source"],
    registry=REGISTRY,
)
WINDOW_ERRORS = Counter(
    "greenkube_collection_window_errors_total",
    "Collection runs stopped before committing a window, by reason.",
    ["source", "reason"],
    registry=REGISTRY,
)
WINDOW_DURATION = Histogram(
    "greenkube_collection_window_duration_seconds",
    "Time spent collecting and writing one collection chunk.",
    ["source"],
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)
DROPPED_SAMPLES = Counter(
    "greenkube_collection_dropped_samples_total",
    "Prometheus samples dropped (non-finite, malformed) or clamped (negative) during range parsing.",
    ["reason"],
    registry=REGISTRY,
)
UNKNOWN_POWER_PROFILE_NODES = Gauge(
    "greenkube_unknown_power_profile_nodes",
    "Nodes whose power profile fell back to DEFAULT_INSTANCE_PROFILE in the latest collection.",
    registry=REGISTRY,
)

K8S_LIST_ERRORS = Counter(
    "greenkube_collection_k8s_list_errors_total",
    "Kubernetes pod or node lists that failed during metric collection, by resource.",
    ["resource"],
    registry=REGISTRY,
)


def record_k8s_list_error(resource: str) -> None:
    """Count a failed Kubernetes list call of the collection pipeline (COL-07)."""
    K8S_LIST_ERRORS.labels(resource=resource if resource in {"pods", "nodes"} else "other").inc()


DEGRADED_WINDOWS = Counter(
    "greenkube_collection_degraded_windows_total",
    "Collection windows stored in degraded mode after COLLECTION_K8S_LIST_MAX_FAILURES consecutive failed "
    "Kubernetes list attempts, by list (rows flagged with reason k8s_list_failed).",
    ["list"],
    registry=REGISTRY,
)
# Every label exists from start-up at 0, so increase() sees the first degraded window.
for _list in ("pods", "nodes", "other"):
    DEGRADED_WINDOWS.labels(list=_list)


def record_degraded_window(resource: str) -> None:
    """Count a window stored without a working Kubernetes pod or node list (review 5)."""
    DEGRADED_WINDOWS.labels(list=resource if resource in {"pods", "nodes"} else "other").inc()


def record_dropped_sample(reason: str, count: int = 1) -> None:
    if count > 0:
        DROPPED_SAMPLES.labels(reason=reason if reason in _DROP_REASONS else DROP_REASON_MALFORMED).inc(count)


def record_gap_windows(source: str, count: int) -> None:
    if count > 0:
        GAP_WINDOWS.labels(source=source).inc(count)


def record_window(source: str, duration_seconds: float) -> None:
    WINDOWS_COLLECTED.labels(source=source).inc()
    WINDOW_DURATION.labels(source=source).observe(max(duration_seconds, 0.0))


def record_window_error(source: str, reason: str) -> None:
    WINDOW_ERRORS.labels(source=source, reason=reason if reason in _ERROR_REASONS else "collection_failed").inc()


def set_watermark_lag(source: str, now: datetime, watermark: datetime | None) -> None:
    if watermark is None:
        return
    WATERMARK_LAG.labels(source=source).set(max((now - watermark).total_seconds(), 0.0))


def set_unknown_power_profile_nodes(count: int) -> None:
    UNKNOWN_POWER_PROFILE_NODES.set(max(count, 0))
