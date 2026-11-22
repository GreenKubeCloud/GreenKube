# src/greenkube/collectors/workload_health_collector.py
"""Workload health signals for post-apply verification (C0.9, BUG-35).

The verifier asks this collector for the health of the pods observed in a
verification window. Signals come from Prometheus, evaluated at the window end
over the window length:

* OOM kills: containers whose ``kube_pod_container_status_last_terminated_reason``
  reported ``OOMKilled`` during the window (kube-state-metrics), and the OOM
  restarts themselves, counted like the scan's ``evidence.oom_events`` (H1).
* Readiness: share of ``kube_pod_status_ready{condition="true"}`` samples that
  were ready over the window, across the observed pods, so each pod weighs its
  lifetime in the window (kube-state-metrics).
* CPU throttling: ``increase(container_cpu_cfs_throttled_periods_total)`` /
  ``increase(container_cpu_cfs_periods_total)`` (cAdvisor), over the workload's
  containers (never the pod cgroup or the pause container). The series only
  exist for containers with a CPU limit.

Queries go to ``/api/v1/query``, falling back to ``/prometheus/api/v1/query``
like the PrometheusCollector.

A signal that cannot be read is returned as ``None`` so the matching gate is
reported ``not_available`` instead of silently passing.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, List, Optional, Sequence

import httpx

from greenkube.models.verification import WorkloadHealthObservation
from greenkube.utils.time import ensure_utc

logger = logging.getLogger(__name__)

#: Step of the OOM restart count: the ``OOM_STEP`` of ``core.reliability_signals``, so the
#: post-change count has the unit of the scan's ``evidence.oom_events`` (H1).
OOM_STEP = "5m"

#: Above this many pods the selector falls back to an owner-name pattern.
MAX_EXPLICIT_PODS = 200

QueryFunction = Callable[[str, datetime], Awaitable[Optional[List[dict[str, Any]]]]]

#: Instant-query API paths, in the order the PrometheusCollector tries them.
_QUERY_PATHS = ("/api/v1/query", "/prometheus/api/v1/query")

_OWNER_POD_PATTERNS = {
    "Deployment": r"{name}-[a-z0-9]{{6,10}}-[a-z0-9]{{5}}",
    "StatefulSet": r"{name}-[0-9]+",
    "DaemonSet": r"{name}-[a-z0-9]{{5}}",
}


def _label_value(value: str) -> str:
    """Escape a value for a double-quoted PromQL label matcher."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def pod_selector(
    namespace: str,
    pods: Sequence[str],
    *,
    owner_kind: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> Optional[str]:
    """Build the ``namespace``/``pod`` label matchers for the observed pods."""
    unique = sorted({pod for pod in pods if pod})
    if unique and len(unique) <= MAX_EXPLICIT_PODS:
        pattern = "|".join(re.escape(pod) for pod in unique)
    elif owner_kind in _OWNER_POD_PATTERNS and owner_name:
        pattern = _OWNER_POD_PATTERNS[owner_kind].format(name=re.escape(owner_name))
    else:
        return None
    return f'namespace="{_label_value(namespace)}", pod=~"{_label_value(pattern)}"'


def _duration(seconds: float) -> str:
    return f"{max(int(math.ceil(seconds)), 60)}s"


def _scalar(results: Optional[List[dict[str, Any]]]) -> Optional[float]:
    """Return the first finite sample of an instant-vector result."""
    for result in results or []:
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        try:
            number = float(value[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


class WorkloadHealthCollector:
    """Reads OOM, readiness and CPU throttling signals from Prometheus."""

    def __init__(self, settings=None, query: Optional[QueryFunction] = None):
        self._settings = settings
        self._query_override = query
        self._client: Optional[httpx.AsyncClient] = None
        #: Query API path that answered last; tried first from then on.
        self._query_path: Optional[str] = None

    @property
    def settings(self):
        if self._settings is not None:
            return self._settings
        from greenkube.core.config import get_config

        return get_config()

    async def collect(
        self,
        *,
        namespace: str,
        pods: Iterable[str],
        container_name: Optional[str],
        start: datetime,
        end: datetime,
        owner_kind: Optional[str] = None,
        owner_name: Optional[str] = None,
    ) -> WorkloadHealthObservation:
        """Return the workload's health over ``[start, end)``."""
        pod_list = sorted({pod for pod in pods if pod})
        selector = pod_selector(namespace, pod_list, owner_kind=owner_kind, owner_name=owner_name)
        observation = WorkloadHealthObservation(pod_count=len(pod_list))
        if selector is None:
            observation.notes.append("no pods observed in the verification window")
            return observation
        if self._query_override is None and not (getattr(self.settings, "PROMETHEUS_URL", "") or "").strip():
            observation.notes.append("Prometheus URL is not configured or discovered")
            return observation
        end = ensure_utc(end)
        window = _duration((end - ensure_utc(start)).total_seconds())
        container_matcher = f', container="{_label_value(container_name)}"' if container_name else ""
        # cAdvisor also exports the pod-level cgroup (container="") and the pause
        # container (container="POD"); summing them with the workload's containers
        # would count the same CFS periods twice.
        cadvisor_matcher = container_matcher or ', container!="", container!="POD"'

        # Read presence over the window: pods replaced before its end still have series.
        ksm_present = _scalar(
            await self._query(f"count(max_over_time(kube_pod_status_ready{{{selector}}}[{window}]))", end)
        )
        if ksm_present is None or ksm_present <= 0:
            observation.notes.append("kube-state-metrics pod series not found")
        else:
            oom = await self._query(
                "count(max_over_time(kube_pod_container_status_last_terminated_reason"
                f'{{{selector}{container_matcher}, reason="OOMKilled"}}[{window}]) > 0)',
                end,
            )
            if oom is not None:
                observation.oom_kill_count = int(_scalar(oom) or 0)
            # OOM restarts in the unit of the scan's evidence (reliability_signals): every
            # restart of a step whose last termination, at that step, is OOMKilled (H1).
            oom_restarts = await self._query(
                "sum(sum_over_time(("
                f"(round(increase(kube_pod_container_status_restarts_total{{{selector}{container_matcher}}}"
                f"[{OOM_STEP}])) > 0) and on (namespace, pod, container) "
                "(kube_pod_container_status_last_terminated_reason"
                f'{{{selector}{container_matcher}, reason="OOMKilled"}} > 0))[{window}:{OOM_STEP}]))',
                end,
            )
            if oom_restarts is not None:
                observation.oom_restart_count = int(round(_scalar(oom_restarts) or 0))
            # Ready samples over present samples across all pods: weighted by pod time, so a
            # short-lived HPA or rollout pod counts for its lifetime, not for 1/N of the ratio.
            ready = f'kube_pod_status_ready{{{selector}, condition="true"}}[{window}]'
            readiness = await self._query(f"sum(sum_over_time({ready})) / sum(count_over_time({ready}))", end)
            observation.readiness_ratio = _scalar(readiness)

        throttled = _scalar(
            await self._query(
                f"sum(increase(container_cpu_cfs_throttled_periods_total{{{selector}{cadvisor_matcher}}}[{window}]))",
                end,
            )
        )
        periods = _scalar(
            await self._query(
                f"sum(increase(container_cpu_cfs_periods_total{{{selector}{cadvisor_matcher}}}[{window}]))",
                end,
            )
        )
        if throttled is not None and periods is not None and periods > 0:
            observation.throttling_ratio = max(0.0, min(throttled / periods, 1.0))
        else:
            observation.notes.append("CPU CFS period series not found (no CPU limit or no cAdvisor data)")
        return observation

    async def _query(self, promql: str, at: datetime) -> Optional[List[dict[str, Any]]]:
        """Run an instant query; ``None`` means the query itself failed."""
        if self._query_override is not None:
            return await self._query_override(promql, at)
        settings = self.settings
        base = (getattr(settings, "PROMETHEUS_URL", "") or "").rstrip("/")
        if not base:
            return None
        headers = {}
        bearer = getattr(settings, "PROMETHEUS_BEARER_TOKEN", None)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        username = getattr(settings, "PROMETHEUS_USERNAME", None)
        password = getattr(settings, "PROMETHEUS_PASSWORD", None)
        auth = (username, password) if username and password else None
        client = await self._get_client(bool(getattr(settings, "PROMETHEUS_VERIFY_CERTS", True)))
        # Same API paths as the PrometheusCollector: some installs serve it behind /prometheus.
        paths = [self._query_path] if self._query_path else list(_QUERY_PATHS)
        payload: Any = None
        last_error: Optional[Exception] = None
        for path in paths:
            try:
                response = await client.get(
                    f"{base}{path}",
                    params={"query": promql, "time": f"{ensure_utc(at).timestamp():.3f}"},
                    headers=headers,
                    auth=auth,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                payload = None
                continue
            if not isinstance(payload, dict):
                last_error = ValueError("Prometheus answered with a non-object payload")
                payload = None
                continue
            self._query_path = path
            break
        if payload is None:
            logger.warning("Health query failed against Prometheus: %s", last_error)
            return None
        if payload.get("status") != "success":
            logger.warning("Prometheus rejected a health query: %s", payload.get("error", "unknown error"))
            return None
        result = payload.get("data", {}).get("result", [])
        return result if isinstance(result, list) else None

    async def _get_client(self, verify: bool) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            from greenkube.utils.http_client import get_async_http_client

            self._client = get_async_http_client(verify=verify)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
