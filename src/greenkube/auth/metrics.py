"""Authentication metrics, exposed by the API on ``/prometheus/metrics``."""

from __future__ import annotations

from prometheus_client import Counter

from greenkube.core.observability import REGISTRY

AUTH_FAILURES = Counter(
    "greenkube_auth_failures_total",
    "Rejected authentication attempts, by reason.",
    ["reason"],
    registry=REGISTRY,
)

FAILURE_REASONS = (
    "bad_credentials",
    "rate_limited",
    "key_rate_limited",
    "invalid_session",
    "invalid_api_key",
    "csrf",
    "forbidden_scope",
    "untrusted_proxy_header",
    "sso",
)


def record_auth_failure(reason: str) -> None:
    AUTH_FAILURES.labels(reason=reason).inc()
