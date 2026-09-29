"""Low-cardinality operational telemetry for the optimization control loop.

The registry is intentionally separate from the API dashboard registry.  This
keeps control-loop metrics available to workers while allowing the API to
append them to its scrape without importing the composition root.
"""

from __future__ import annotations

import logging
from time import monotonic
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
