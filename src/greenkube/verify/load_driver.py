# src/greenkube/verify/load_driver.py
"""Load drivers for the measurement counterfactual (C3).

A load driver is an hourly series of how much work the workload was asked to
do. :class:`LoadDriverResolver` tries the candidates in this order and keeps
the first one whose coverage reaches the minimum in **both** the pre- and the
post-change window:

1. requests per second from the service mesh or ingress (Istio
   ``istio_requests_total``, Linkerd ``response_total``, ingress-nginx
   ``nginx_ingress_controller_requests``, then Envoy
   ``envoy_cluster_upstream_rq_total`` for Contour-style
   ``<namespace>_<service>_<port>`` clusters; the ingress and Envoy drivers
   assume a Service named after the workload);
2. the PromQL expression in the workload annotation
   ``greenkube.io/load-metric`` (``$namespace`` and ``$workload`` are
   substituted);
3. the workload's CPU usage, from GreenKube's own metrics (weakest: a CPU
   change can move it; it is flagged ``weak``).

Prometheus drivers are read as hourly means (``avg_over_time(...[1h:5m])``)
through ``/api/v1/query_range``. When Prometheus is not configured only the
CPU-usage driver is available.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

import httpx

from greenkube.verify.model import ChangeEvent, HourBucket, LoadDriverInfo, TimeWindow
from greenkube.verify.series import floor_hour, quantity_series, window_hours

logger = logging.getLogger(__name__)

#: Workload annotation holding a custom load-driver PromQL expression.
LOAD_METRIC_ANNOTATION = "greenkube.io/load-metric"

CPU_USAGE_DRIVER = "cpu_usage"

RangeQuery = Callable[[str, datetime, datetime, int], Awaitable[Optional[List[dict[str, Any]]]]]

_RANGE_PATHS = ("/api/v1/query_range", "/prometheus/api/v1/query_range")


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _regex_label(value: str) -> str:
    """``value`` as a literal inside a PromQL (RE2) regular expression label matcher."""
    return _label(re.sub(r"([\\.+*?()|\[\]{}^$])", r"\\\1", value))


@dataclass(frozen=True)
class LoadDriverCandidate:
    key: str
    label: str
    promql: Optional[str] = None
    weak: bool = False


def candidates_for(change: ChangeEvent) -> List[LoadDriverCandidate]:
    """Load-driver candidates of a change, in resolution order."""
    namespace = _label(change.target.namespace)
    name = _label(change.target.name)
    kind = change.target.kind.lower()
    found = [
        LoadDriverCandidate(
            key="istio_rps",
            label="Istio requests/s",
            promql=(
                'sum(rate(istio_requests_total{reporter="destination", '
                f'destination_workload_namespace="{namespace}", destination_workload="{name}"}}[5m]))'
            ),
        ),
        LoadDriverCandidate(
            key="linkerd_rps",
            label="Linkerd requests/s",
            promql=f'sum(rate(response_total{{direction="inbound", namespace="{namespace}", {kind}="{name}"}}[5m]))',
        ),
        LoadDriverCandidate(
            key="ingress_nginx_rps",
            label="ingress-nginx requests/s (Service named after the workload)",
            promql=(
                "sum(rate(nginx_ingress_controller_requests{"
                f'exported_namespace="{namespace}", exported_service="{name}"}}[5m]))'
            ),
        ),
        LoadDriverCandidate(
            key="envoy_rps",
            label="Envoy upstream requests/s (Contour-style cluster of a Service named after the workload)",
            promql=(
                "sum(rate(envoy_cluster_upstream_rq_total{envoy_cluster_name=~"
                f'"{_regex_label(change.target.namespace)}_{_regex_label(change.target.name)}_[^_]+"}}[5m]))'
            ),
        ),
    ]
    custom = (change.load_metric or "").strip()
    if custom:
        expression = custom.replace("$namespace", change.target.namespace).replace("$workload", change.target.name)
        found.append(
            LoadDriverCandidate(key="annotation", label=f"{LOAD_METRIC_ANNOTATION} annotation", promql=expression)
        )
    found.append(LoadDriverCandidate(key=CPU_USAGE_DRIVER, label="workload CPU usage", weak=True))
    return found


class PrometheusRangeReader:
    """Reads hourly means of a PromQL expression (``None`` when Prometheus is unavailable)."""

    def __init__(self, settings=None, query_range: Optional[RangeQuery] = None):
        self._settings = settings
        self._override = query_range
        self._client: Optional[httpx.AsyncClient] = None
        self._path: Optional[str] = None

    @property
    def settings(self):
        if self._settings is not None:
            return self._settings
        from greenkube.core.config import get_config

        return get_config()

    @property
    def available(self) -> bool:
        return self._override is not None or bool((getattr(self.settings, "PROMETHEUS_URL", "") or "").strip())

    async def hourly_means(self, promql: str, window: TimeWindow) -> Optional[Dict[datetime, float]]:
        """``{hour: mean}`` over ``window``; ``None`` when the query failed."""
        start = floor_hour(window.start) + timedelta(hours=1)
        end = floor_hour(window.end)
        if end < start:
            return {}
        result = await self._query_range(f"avg_over_time(({promql})[1h:5m])", start, end, 3600)
        if result is None:
            return None
        values: Dict[datetime, float] = {}
        for series in result:
            points = series.get("values") if isinstance(series, dict) else None
            for point in points or []:
                try:
                    moment = datetime.fromtimestamp(float(point[0]), tz=timezone.utc)
                    number = float(point[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if not math.isfinite(number) or number < 0:
                    continue
                # The sample at t averages [t - 1h, t): it describes the hour before t.
                hour = floor_hour(moment) - timedelta(hours=1)
                values[hour] = values.get(hour, 0.0) + number
        return values

    async def _query_range(
        self, promql: str, start: datetime, end: datetime, step: int
    ) -> Optional[List[dict[str, Any]]]:
        if self._override is not None:
            return await self._override(promql, start, end, step)
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
        if self._client is None or self._client.is_closed:
            from greenkube.utils.http_client import get_async_http_client

            self._client = get_async_http_client(verify=bool(getattr(settings, "PROMETHEUS_VERIFY_CERTS", True)))
        params = {"query": promql, "start": f"{start.timestamp():.0f}", "end": f"{end.timestamp():.0f}", "step": step}
        last_error: Optional[Exception] = None
        for path in [self._path] if self._path else list(_RANGE_PATHS):
            try:
                response = await self._client.get(f"{base}{path}", params=params, headers=headers, auth=auth)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                continue
            if not isinstance(payload, dict) or payload.get("status") != "success":
                last_error = ValueError(str(payload.get("error")) if isinstance(payload, dict) else "bad payload")
                continue
            self._path = path
            result = payload.get("data", {}).get("result", [])
            return result if isinstance(result, list) else None
        logger.info("Load-driver query failed against Prometheus: %s", last_error)
        return None

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def coverage(values: Mapping[datetime, float], window: TimeWindow) -> float:
    hours = window_hours(window)
    if not hours:
        return 0.0
    return sum(1 for hour in hours if hour in values) / len(hours)


@dataclass
class CollectedLoad:
    """Candidate load-driver series of one window, and the candidates whose query failed.

    ``unavailable`` lists the Prometheus candidates whose query failed (as
    opposed to returning no series): their absence says nothing about the
    workload, so evidence is not frozen without them while a retry may succeed.
    """

    series: Dict[str, Dict[datetime, float]] = field(default_factory=dict)
    unavailable: List[str] = field(default_factory=list)


@dataclass
class ResolvedLoad:
    """The selected load driver and its hourly series in both windows."""

    info: LoadDriverInfo
    pre: Dict[datetime, float] = field(default_factory=dict)
    post: Dict[datetime, float] = field(default_factory=dict)


class LoadDriverResolver:
    """Selects the load driver of a change measurement (C3)."""

    def __init__(self, reader: Optional[PrometheusRangeReader] = None, min_coverage: float = 0.8):
        self.reader = reader
        self.min_coverage = min_coverage

    async def series(
        self, change: ChangeEvent, window: TimeWindow, buckets: Sequence[HourBucket]
    ) -> Dict[str, Dict[datetime, float]]:
        """Hourly series of every available candidate over ``window`` (stored in the baseline snapshot)."""
        return (await self.collect(change, window, buckets)).series

    async def collect(self, change: ChangeEvent, window: TimeWindow, buckets: Sequence[HourBucket]) -> CollectedLoad:
        """Like :meth:`series`, also naming the Prometheus candidates whose query failed."""
        collected = CollectedLoad()
        for candidate in candidates_for(change):
            if candidate.promql is None:
                values = quantity_series(buckets, "cpu_usage_cores")
            elif self.reader is not None and self.reader.available:
                try:
                    values = await self.reader.hourly_means(candidate.promql, window)
                except Exception as exc:
                    logger.info("Load driver %s unavailable for change %s: %s", candidate.key, change.change_id, exc)
                    values = None
                if values is None:
                    collected.unavailable.append(candidate.key)
            else:
                values = None
            if values:
                collected.series[candidate.key] = values
        return collected

    def resolve(
        self,
        change: ChangeEvent,
        pre_window: TimeWindow,
        post_window: TimeWindow,
        pre: Mapping[str, Mapping[datetime, float]],
        post: Mapping[str, Mapping[datetime, float]],
    ) -> Optional[ResolvedLoad]:
        """First candidate with enough coverage and a positive mean in both windows."""
        for candidate in candidates_for(change):
            pre_values = dict(pre.get(candidate.key) or {})
            post_values = dict(post.get(candidate.key) or {})
            pre_cover = coverage(pre_values, pre_window)
            post_cover = coverage(post_values, post_window)
            if pre_cover < self.min_coverage or post_cover < self.min_coverage:
                continue
            if not any(value > 0 for value in pre_values.values()):
                continue
            return ResolvedLoad(
                info=LoadDriverInfo(
                    key=candidate.key,
                    label=candidate.label,
                    coverage_pre=round(pre_cover, 4),
                    coverage_post=round(post_cover, 4),
                    weak=candidate.weak,
                    detail=candidate.promql or "GreenKube CPU usage (cores)",
                ),
                pre=pre_values,
                post=post_values,
            )
        return None
