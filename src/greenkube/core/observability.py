"""Low-cardinality operational telemetry for the optimization control loop.

The registry is intentionally separate from the API dashboard registry.  This
keeps control-loop metrics available to workers while allowing the API to
append them to its scrape without importing the composition root.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from math import isfinite
from time import monotonic, time
from typing import Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

OPTIMIZATION_RUNS = Counter(
    "greenkube_optimization_runs_total",
    "Optimization runs by terminal status.",
    ["status"],
    registry=REGISTRY,
)
OPTIMIZATION_DURATION = Histogram(
    "greenkube_optimization_run_duration_seconds",
    "Optimization run duration in seconds.",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
ANALYZER_FAILURES = Counter(
    "greenkube_optimization_analyzer_failures_total",
    "Recommendation source failures by source name.",
    ["source"],
    registry=REGISTRY,
)
RECOMMENDATION_SCAN_WINDOW = Gauge(
    "greenkube_recommendation_scan_window_seconds",
    "Effective lookback window used by the latest recommendation scan.",
    registry=REGISTRY,
)
RECOMMENDATION_SCAN_ROWS = Gauge(
    "greenkube_recommendation_scan_metric_rows",
    "Combined and container metric rows materialized by the latest recommendation scan.",
    registry=REGISTRY,
)
RECOMMENDATION_SCAN_RESIDENT_MEMORY = Gauge(
    "greenkube_recommendation_scan_resident_memory_bytes",
    "Process peak resident memory observed when the latest recommendation scan was built.",
    registry=REGISTRY,
)
RECOMMENDATIONS_GENERATED = Counter(
    "greenkube_recommendations_generated_total",
    "Recommendations emitted by completed optimization runs.",
    registry=REGISTRY,
)
VERIFICATION_OUTCOMES = Counter(
    "greenkube_verification_outcomes_total",
    "Verification outcomes, including rollback reviews.",
    ["outcome"],
    registry=REGISTRY,
)
HEALTH_GATE_FAILURES = Counter(
    "greenkube_verification_health_gate_failures_total",
    "Verification health-gate failures by gate.",
    ["gate"],
    registry=REGISTRY,
)
LAST_SUCCESSFUL_RUN = Gauge(
    "greenkube_last_successful_optimization_run_timestamp_seconds",
    "Unix timestamp of the last fully successful optimization run.",
    registry=REGISTRY,
)

GITOPS_PR_POLL_CYCLES = Counter(
    "greenkube_gitops_pr_poll_cycles_total",
    "Bounded Git provider pull-request polling cycles by outcome.",
    ["outcome"],
    registry=REGISTRY,
)
GITOPS_PR_POLL_LEASE_CONTENTIONS = Counter(
    "greenkube_gitops_pr_poll_lease_contentions_total",
    "Polling cycles skipped because another worker owns the singleton lease.",
    registry=REGISTRY,
)
GITOPS_PR_POLL_ERRORS = Counter(
    "greenkube_gitops_pr_poll_errors_total",
    "Errors while reconciling or advancing the bounded pull-request poll queue.",
    registry=REGISTRY,
)
GITOPS_PR_POLL_DURATION = Histogram(
    "greenkube_gitops_pr_poll_duration_seconds",
    "Duration of a bounded Git provider pull-request polling cycle.",
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120, 300),
    registry=REGISTRY,
)

AUTOMATION_PLANS = Counter(
    "greenkube_automation_plans_total",
    "Plan creation outcomes by bounded status and renderer.",
    ["status", "renderer"],
    registry=REGISTRY,
)
AUTOMATION_PLAN_DURATION = Histogram(
    "greenkube_automation_plan_duration_seconds",
    "Duration of plan creation and render validation by renderer.",
    ["renderer"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    registry=REGISTRY,
)
AUTOMATION_OPERATIONS = Counter(
    "greenkube_automation_operations_total",
    "Terminal plan operation outcomes by status and bounded operation type.",
    ["status", "type"],
    registry=REGISTRY,
)
AUTOMATION_OPERATION_DURATION = Histogram(
    "greenkube_automation_operation_duration_seconds",
    "Plan operation execution duration by bounded operation type.",
    ["type"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 900, 1800),
    registry=REGISTRY,
)
AUTOMATION_QUEUE_AGE = Histogram(
    "greenkube_automation_queue_age_seconds",
    "Age of a plan operation when claimed by a worker.",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 86400),
    registry=REGISTRY,
)
AUTOMATION_WORKER_UP = Gauge(
    "greenkube_automation_worker_up",
    "Whether the automation worker loop is running in this process.",
    registry=REGISTRY,
)
AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP = Gauge(
    "greenkube_automation_worker_heartbeat_timestamp_seconds",
    "Unix timestamp of the most recent automation worker loop heartbeat.",
    registry=REGISTRY,
)
AUTOMATION_WORKER_HEARTBEAT_AGE = Gauge(
    "greenkube_automation_worker_heartbeat_age_seconds",
    "Age of the most recent persisted automation worker heartbeat; -1 means none exists.",
    registry=REGISTRY,
)
AUTOMATION_BACKLOG = Gauge(
    "greenkube_automation_backlog",
    "Automation queue, stuck-state and worker-health counts by bounded queue/state.",
    ["queue", "state"],
    registry=REGISTRY,
)
AUTOMATION_FAILURES = Counter(
    "greenkube_automation_failures_total",
    "Automation lifecycle failures by bounded subsystem category.",
    ["reason"],
    registry=REGISTRY,
)

PULL_REQUESTS = Counter(
    "greenkube_pull_requests_total",
    "Pull-request lifecycle states observed by provider type.",
    ["state", "provider"],
    registry=REGISTRY,
)
PULL_REQUEST_MERGE_DETECTION = Histogram(
    "greenkube_pull_request_merge_detection_seconds",
    "Elapsed time from provider-reported merge time to lifecycle detection.",
    buckets=(1, 10, 30, 60, 180, 300, 900, 1800, 3600, 86400),
    registry=REGISTRY,
)

WEBHOOK_DELIVERIES = Counter(
    "greenkube_webhook_deliveries_total",
    "Webhook delivery outcomes by bounded processing status.",
    ["status"],
    registry=REGISTRY,
)
WEBHOOK_DELIVERY_LAG = Histogram(
    "greenkube_webhook_delivery_lag_seconds",
    "Time from webhook ingress to a terminal HTTP response.",
    buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)

GITOPS_DISCOVERY_REFRESHES = Counter(
    "greenkube_gitops_discovery_refreshes_total",
    "GitOps discovery refreshes by success status.",
    ["status"],
    registry=REGISTRY,
)
GITOPS_BINDINGS = Gauge(
    "greenkube_gitops_bindings_total",
    "Current GitOps workload bindings by resolution status.",
    ["status"],
    registry=REGISTRY,
)
GITOPS_COVERAGE_RATIO = Gauge(
    "greenkube_gitops_coverage_ratio",
    "Fraction of discovered GitOps workload bindings that are resolved.",
    registry=REGISTRY,
)
GITOPS_DISCOVERY_REFRESH_DURATION = Histogram(
    "greenkube_gitops_discovery_refresh_duration_seconds",
    "Duration of a GitOps binding discovery refresh.",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
GITOPS_SOURCE_AGE = Gauge(
    "greenkube_gitops_source_age_seconds",
    "Age of the oldest source observation in the latest successful discovery refresh.",
    registry=REGISTRY,
)
GITOPS_DISCOVERY_LAST_SUCCESS = Gauge(
    "greenkube_gitops_discovery_last_success_timestamp_seconds",
    "Unix timestamp of the latest successful GitOps discovery refresh.",
    registry=REGISTRY,
)

GITOPS_SYNC_OUTCOMES = Counter(
    "greenkube_gitops_sync_outcomes_total",
    "Plan sync observation outcomes.",
    ["outcome"],
    registry=REGISTRY,
)
GITOPS_SYNC_TIMEOUTS = Counter(
    "greenkube_gitops_sync_timeouts_total",
    "Plans that reached their configured GitOps sync timeout.",
    registry=REGISTRY,
)
GITOPS_SYNC_LAG = Histogram(
    "greenkube_gitops_sync_lag_seconds",
    "Elapsed time from pull-request merge to terminal GitOps sync observation.",
    buckets=(30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 86400),
    registry=REGISTRY,
)

_PLAN_STATUSES = frozenset(
    {
        "draft",
        "validated",
        "approved",
        "applying",
        "pr_open",
        "merged",
        "synced",
        "applied",
        "sync_failed",
        "closed",
        "stale",
        "failed",
        "needs_mapping",
        "ambiguous",
        "rejected",
        "superseded",
        "cancelled",
    }
)
_RENDERERS = frozenset({"raw", "kustomize", "helm_values", "helm_release", "unknown"})
_OPERATION_STATUSES = frozenset({"queued", "running", "retrying", "succeeded", "failed", "cancelled", "unknown"})
_OPERATION_TYPES = frozenset({"apply", "rollback", "unknown"})
_PR_STATES = frozenset({"open", "merged", "closed", "unchanged", "unknown"})
_PR_PROVIDERS = frozenset({"github", "gitlab", "gitea", "other"})
_WEBHOOK_STATUSES = frozenset(
    {"received", "processed", "duplicate", "retryable", "reconcile_unavailable", "rejected", "unavailable", "error"}
)
_BINDING_STATUSES = frozenset({"resolved", "ambiguous", "unresolved", "blocked", "stale"})
_DISCOVERY_REFRESH_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_SYNC_OUTCOMES = frozenset({"synced", "pending", "failed", "timeout", "skipped", "error"})
_FAILURE_REASONS = frozenset({"plan", "worker", "polling", "webhook", "discovery", "sync", "other"})

AUTOMATION_WORKER_UP.set(0)
AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP.set(0)
GITOPS_COVERAGE_RATIO.set(0)
GITOPS_SOURCE_AGE.set(0)
GITOPS_DISCOVERY_LAST_SUCCESS.set(0)
for _binding_status in _BINDING_STATUSES:
    GITOPS_BINDINGS.labels(status=_binding_status).set(0)


_AUDIT_LOG = logging.getLogger("greenkube.audit")


def record_analyzer_failure(source: str) -> None:
    ANALYZER_FAILURES.labels(source=source[:128]).inc()


def record_run(status: str, duration_seconds: float, recommendation_count: int = 0) -> None:
    """Record a terminal run and emit a compact audit event."""
    safe_status = status if status in {"succeeded", "failed"} else "failed"
    OPTIMIZATION_RUNS.labels(status=safe_status).inc()
    OPTIMIZATION_DURATION.observe(max(duration_seconds, 0.0))
    if recommendation_count > 0:
        RECOMMENDATIONS_GENERATED.inc(recommendation_count)
    if safe_status == "succeeded":
        from time import time

        LAST_SUCCESSFUL_RUN.set(time())
    _AUDIT_LOG.info(
        "optimization_run_completed",
        extra={
            "audit_event": "optimization_run_completed",
            "status": safe_status,
            "duration_seconds": round(max(duration_seconds, 0.0), 3),
            "recommendation_count": recommendation_count,
        },
    )


def record_recommendation_scan_resources(
    window_seconds: float,
    metric_rows: int,
    resident_memory_bytes: int,
) -> None:
    """Expose the bounded scan window, loaded rows and process peak RSS."""
    RECOMMENDATION_SCAN_WINDOW.set(max(0.0, float(window_seconds)))
    RECOMMENDATION_SCAN_ROWS.set(max(0, int(metric_rows)))
    RECOMMENDATION_SCAN_RESIDENT_MEMORY.set(max(0, int(resident_memory_bytes)))


def record_verification(outcome: str, failed_gates: Optional[list[str]] = None) -> None:
    """Record a verification verdict and its failed health gates."""
    VERIFICATION_OUTCOMES.labels(outcome=outcome[:64]).inc()
    for gate in failed_gates or []:
        HEALTH_GATE_FAILURES.labels(gate=gate[:64]).inc()
    _AUDIT_LOG.info(
        "verification_completed",
        extra={
            "audit_event": "verification_completed",
            "outcome": outcome,
            "failed_gates": list(failed_gates or []),
        },
    )


def generate_latest_observability() -> bytes:
    """Render control-loop metrics in Prometheus text format."""
    return generate_latest(REGISTRY)


def start_timer() -> float:
    return monotonic()


def elapsed(timer: float) -> float:
    return monotonic() - timer


def record_automation_plan(status: str, renderer: str, duration_seconds: float) -> None:
    safe_renderer = _bounded_label(renderer, _RENDERERS, "unknown")
    safe_status = _bounded_label(status, _PLAN_STATUSES, "failed")
    AUTOMATION_PLANS.labels(status=safe_status, renderer=safe_renderer).inc()
    AUTOMATION_PLAN_DURATION.labels(renderer=safe_renderer).observe(_nonnegative_seconds(duration_seconds))


def record_plan_operation(status: str, operation_type: str) -> None:
    AUTOMATION_OPERATIONS.labels(
        status=_bounded_label(status, _OPERATION_STATUSES, "unknown"),
        type=_bounded_label(operation_type, _OPERATION_TYPES, "unknown"),
    ).inc()


def record_plan_operation_duration(operation_type: str, duration_seconds: float) -> None:
    AUTOMATION_OPERATION_DURATION.labels(type=_bounded_label(operation_type, _OPERATION_TYPES, "unknown")).observe(
        _nonnegative_seconds(duration_seconds)
    )


def record_automation_queue_age(age_seconds: float) -> None:
    AUTOMATION_QUEUE_AGE.observe(_nonnegative_seconds(age_seconds))


def set_automation_worker_up(up: bool) -> None:
    AUTOMATION_WORKER_UP.set(1 if up is True else 0)


def record_automation_worker_heartbeat(timestamp: float) -> None:
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or timestamp < 0
        or not isfinite(timestamp)
    ):
        raise ValueError("worker heartbeat timestamp must be a non-negative number")
    AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP.set(timestamp)


def record_automation_backlog(metrics: Mapping[str, object]) -> None:
    """Publish bounded database-backed automation backlog and freshness gauges."""
    bounded_values = {
        ("operations", "queued"): "queued_operations",
        ("operations", "running"): "running_operations",
        ("operations", "retrying"): "retrying_operations",
        ("operations", "failed"): "failed_operations",
        ("operations", "stuck"): "stuck_operations",
        ("plans", "applying"): "applying_plans",
        ("plans", "merged"): "merged_plans",
        ("plans", "stuck"): "stuck_plans",
        ("plans", "sync_failed"): "sync_failed_plans",
        ("webhooks", "pending"): "pending_webhooks",
        ("webhooks", "stuck"): "stuck_webhooks",
        ("workers", "total"): "worker_count",
        ("workers", "stale"): "stale_workers",
    }
    for (queue, state), key in bounded_values.items():
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
            raise ValueError(f"automation backlog metric {key!r} must be a finite non-negative number")
        AUTOMATION_BACKLOG.labels(queue=queue, state=state).set(value)

    timestamp = metrics.get("latest_worker_heartbeat_timestamp")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not isfinite(timestamp)
        or timestamp < 0
    ):
        raise ValueError("latest worker heartbeat timestamp must be a finite non-negative number")
    if timestamp > 0:
        record_automation_worker_heartbeat(float(timestamp))
        AUTOMATION_WORKER_HEARTBEAT_AGE.set(max(0.0, time() - float(timestamp)))
    else:
        AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP.set(0)
        AUTOMATION_WORKER_HEARTBEAT_AGE.set(-1)


def record_automation_failure(reason: str) -> None:
    AUTOMATION_FAILURES.labels(reason=_bounded_label(reason, _FAILURE_REASONS, "other")).inc()


def record_pull_request_state(state: str, provider: str) -> None:
    PULL_REQUESTS.labels(
        state=_bounded_label(state, _PR_STATES, "unknown"),
        provider=_bounded_label(provider, _PR_PROVIDERS, "other"),
    ).inc()


def record_pull_request_merge_detection(lag_seconds: float) -> None:
    PULL_REQUEST_MERGE_DETECTION.observe(_nonnegative_seconds(lag_seconds))


def record_pr_poll_cycle(outcome: str, duration_seconds: float) -> None:
    GITOPS_PR_POLL_CYCLES.labels(
        outcome=_bounded_label(outcome, frozenset({"completed", "lease_contended", "error", "cancelled"}), "error")
    ).inc()
    GITOPS_PR_POLL_DURATION.observe(_nonnegative_seconds(duration_seconds))


def record_pr_poll_lease_contention() -> None:
    GITOPS_PR_POLL_LEASE_CONTENTIONS.inc()


def record_pr_poll_error() -> None:
    GITOPS_PR_POLL_ERRORS.inc()


def record_webhook_delivery(status: str, lag_seconds: float) -> None:
    WEBHOOK_DELIVERIES.labels(status=_bounded_label(status, _WEBHOOK_STATUSES, "error")).inc()
    WEBHOOK_DELIVERY_LAG.observe(_nonnegative_seconds(lag_seconds))


def record_gitops_discovery_refresh(
    *,
    status: str,
    duration_seconds: float,
    binding_counts: Mapping[str, int] | None = None,
    coverage_ratio: float | None = None,
    source_age_seconds: float | None = None,
    refreshed_at_timestamp: float | None = None,
) -> None:
    safe_status = _bounded_label(status, _DISCOVERY_REFRESH_STATUSES, "failed")
    GITOPS_DISCOVERY_REFRESHES.labels(status=safe_status).inc()
    GITOPS_DISCOVERY_REFRESH_DURATION.observe(_nonnegative_seconds(duration_seconds))
    if safe_status != "succeeded":
        return

    counts = binding_counts or {}
    for binding_status in _BINDING_STATUSES:
        GITOPS_BINDINGS.labels(status=binding_status).set(_nonnegative_count(counts.get(binding_status, 0)))
    GITOPS_COVERAGE_RATIO.set(_bounded_ratio(coverage_ratio))
    GITOPS_SOURCE_AGE.set(_nonnegative_seconds(source_age_seconds))
    GITOPS_DISCOVERY_LAST_SUCCESS.set(_nonnegative_seconds(refreshed_at_timestamp))


def record_sync_outcome(outcome: str) -> None:
    GITOPS_SYNC_OUTCOMES.labels(outcome=_bounded_label(outcome, _SYNC_OUTCOMES, "error")).inc()


def record_sync_timeout() -> None:
    GITOPS_SYNC_TIMEOUTS.inc()


def record_sync_lag(lag_seconds: float) -> None:
    GITOPS_SYNC_LAG.observe(_nonnegative_seconds(lag_seconds))


def _bounded_label(value: object, allowed: frozenset[str], fallback: str) -> str:
    normalized = getattr(value, "value", value)
    if isinstance(normalized, str):
        normalized = normalized.casefold()
    return normalized if isinstance(normalized, str) and normalized in allowed else fallback


def _nonnegative_seconds(value: float | None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return 0.0
    return max(0.0, float(value))


def _nonnegative_count(value: int) -> int:
    if type(value) is not int:
        return 0
    return max(0, value)


def _bounded_ratio(value: float | None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return 0.0
    return min(1.0, max(0.0, float(value)))
