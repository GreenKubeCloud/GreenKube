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
#: A value the node cannot produce (counter-start CPU above the node's cores, review 1).
DROP_REASON_IMPLAUSIBLE = "implausible"
_DROP_REASONS = frozenset(
    {
        DROP_REASON_NON_FINITE,
        DROP_REASON_NEGATIVE,
        DROP_REASON_MALFORMED,
        DROP_REASON_OUT_OF_WINDOW,
        DROP_REASON_IMPLAUSIBLE,
    }
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


COVERAGE_RATIO = Gauge(
    "greenkube_collection_coverage_ratio",
    "Share of the steps of the backfill horizon whose data is known (collected, or confirmed empty), "
    "read from the collection_windows table after each run (COL-04).",
    ["source"],
    registry=REGISTRY,
)
STEPS_RECORDED = Counter(
    "greenkube_collection_steps_total",
    "Collection steps recorded in collection_windows, by status (collected, empty, failed, gap).",
    ["source", "status"],
    registry=REGISTRY,
)
_STEP_STATUSES = frozenset({"collected", "empty", "failed", "gap"})
COST_UNATTRIBUTED_RATIO = Gauge(
    "greenkube_collection_cost_unattributed_ratio",
    "Share of the OpenCost allocation total of the last collected window that no stored pod row carries "
    "(pods without any CPU, memory or Running sample), COL-03.",
    ["source"],
    registry=REGISTRY,
)


IDENTITY_MISMATCH = Gauge(
    "greenkube_collection_identity_mismatch",
    "1 when the database belongs to another cluster (installation kube-system UID differs) and collection "
    "refuses to start; 0 once the identity is accepted.",
    registry=REGISTRY,
)


def set_identity_mismatch(mismatch: bool) -> None:
    """Publish whether collection is refused for a cluster identity mismatch (COL-02 review)."""
    IDENTITY_MISMATCH.set(1 if mismatch else 0)


RATE_WINDOW_SCRAPES = Gauge(
    "greenkube_collection_rate_window_scrapes",
    "Most scrapes any cAdvisor CPU series holds inside the rate window, measured hourly (COL-04). "
    "Below 2, rate() returns nothing: PROMETHEUS_SCRAPE_INTERVAL is shorter than the real scrape interval.",
    ["source"],
    registry=REGISTRY,
)


def set_rate_window_scrapes(source: str, scrapes: float) -> None:
    """Publish the measured scrapes per rate window (COL-04 step 3)."""
    RATE_WINDOW_SCRAPES.labels(source=source).set(max(scrapes, 0.0))


COST_PENDING_STEPS = Gauge(
    "greenkube_collection_cost_pending_steps",
    "Collection steps inside the backfill horizon whose cost waits for OpenCost (outage, lag or rejected "
    "request): their rows carry a modeled cost labelled opencost_pending, COST-01.",
    ["source"],
    registry=REGISTRY,
)


COST_PENDING_EXPIRED = Counter(
    "greenkube_collection_cost_pending_expired_steps_total",
    "Collection steps that left COLLECTION_MAX_BACKFILL while their cost still waited for OpenCost; their rows "
    "keep an untrusted opencost_pending cost (COST-01).",
    ["source"],
    registry=REGISTRY,
)


def record_cost_pending_expired(source: str, steps: int) -> None:
    """Count pending steps that left the backfill horizon (COST-01)."""
    if steps > 0:
        COST_PENDING_EXPIRED.labels(source=source).inc(steps)


def set_cost_pending_steps(source: str, steps: int) -> None:
    """Publish the number of steps whose cost waits for OpenCost (COST-01)."""
    COST_PENDING_STEPS.labels(source=source).set(max(int(steps), 0))


def set_cost_unattributed_ratio(source: str, ratio: float) -> None:
    """Publish the unattributed share of the OpenCost window total (COL-03 step 4)."""
    COST_UNATTRIBUTED_RATIO.labels(source=source).set(min(max(ratio, 0.0), 1.0))


def record_steps(source: str, status: str, count: int) -> None:
    """Count steps recorded with ``status`` (COL-04)."""
    if count > 0:
        STEPS_RECORDED.labels(source=source, status=status if status in _STEP_STATUSES else "failed").inc(count)


def set_coverage_ratio(source: str, ratio: float) -> None:
    """Publish the coverage of the backfill horizon (COL-04)."""
    COVERAGE_RATIO.labels(source=source).set(min(max(ratio, 0.0), 1.0))


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


#: Reasons a re-collected window was left as stored (STO-01).
RECOLLECT_REASON_FEWER_PODS = "missing_pods"
RECOLLECT_REASON_COST_UNAVAILABLE = "cost_unavailable"
RECOLLECT_SKIPPED = Counter(
    "greenkube_collection_recollect_skipped_total",
    "Re-collected windows not written because the new pass was worse than the stored rows, by reason.",
    ["source", "reason"],
    registry=REGISTRY,
)


def record_recollect_skipped(source: str, reason: str) -> None:
    RECOLLECT_SKIPPED.labels(source=source, reason=reason).inc()


#: Reasons a collected row was left out of its window (STO-03).
REJECT_REASON_NO_TIMESTAMP = "no_timestamp"
ROWS_REJECTED = Counter(
    "greenkube_collection_rows_rejected_total",
    "Collected rows left out of their window because they cannot be stored, by reason.",
    ["source", "reason"],
    registry=REGISTRY,
)


def record_rows_rejected(source: str, reason: str, count: int = 1) -> None:
    if count > 0:
        ROWS_REJECTED.labels(source=source, reason=reason).inc(count)


#: Outcome of the last hourly intensity fetch of a zone (NRG-03): fetched, empty or failed.
INTENSITY_ZONES = Gauge(
    "greenkube_intensity_zones",
    "Zones of the hourly carbon intensity job; 1 on the status of the latest fetch of the zone.",
    ["zone", "status"],
    registry=REGISTRY,
)
INTENSITY_ZONE_STATUSES = ("fetched", "empty", "failed")


def set_intensity_zone_status(zone: str, status: str) -> None:
    """Marks ``status`` as the outcome of the latest intensity fetch of ``zone``."""
    for candidate in INTENSITY_ZONE_STATUSES:
        INTENSITY_ZONES.labels(zone=zone, status=candidate).set(1 if candidate == status else 0)
