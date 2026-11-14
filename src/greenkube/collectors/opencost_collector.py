import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import httpx

# from requests.packages.urllib3.exceptions import InsecureRequestWarning
# Removing InsecureRequestWarning import if not used by httpx directly or handle differently.
# Httpx doesn't emit InsecureRequestWarning in same way usually, just ignore verify=False.
from greenkube.collectors.discovery.base import BaseDiscovery
from greenkube.collectors.discovery.opencost import OpenCostDiscovery

from ..core.config import config
from ..core.observability import elapsed, record_collection_request, start_timer
from ..models.metrics import CostMetric
from ..models.provenance import CostSource
from ..utils.http_client import get_async_http_client
from .base_collector import BaseCollector

logger = logging.getLogger(__name__)


def _optional_number(value: Any) -> Optional[float]:
    """A finite float from an OpenCost JSON value; ``None`` when missing, NaN, infinite or not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: Any) -> float:
    """Return a finite float from an OpenCost JSON value (``None``/NaN become 0)."""
    number = _optional_number(value)
    return 0.0 if number is None else number


def untrusted_cost_reason(item: dict) -> Optional[str]:
    """Why an OpenCost allocation's cost must not back a measurement (``None`` when it can).

    A missing cost is missing, never a zero (fail closed): ``totalCost``,
    ``cpuCost`` or ``ramCost`` present but NaN, infinite or not a number, a
    total that is neither reported nor derivable from the CPU and RAM costs,
    and a zero total for a pod that ran (``minutes > 0``) with non-zero
    requests (OpenCost had no price for it yet) are all untrusted. Rows built
    from such an allocation carry ``cost_source = none``, so a NaN never
    becomes a measured saving.
    """
    for key in ("totalCost", "cpuCost", "ramCost"):
        if key in item and item[key] is not None and _optional_number(item[key]) is None:
            return f"OpenCost {key} is not a finite number ({item[key]!r})"
    total = _optional_number(item.get("totalCost"))
    if total is None and (
        _optional_number(item.get("cpuCost")) is None or _optional_number(item.get("ramCost")) is None
    ):
        return "OpenCost reported no total, CPU or RAM cost"
    minutes = _optional_number(item.get("minutes"))
    requested = any(
        (_optional_number(item.get(key)) or 0.0) > 0 for key in ("cpuCoreRequestAverage", "ramByteRequestAverage")
    )
    if total is not None and total == 0 and minutes is not None and minutes > 0 and requested:
        return "OpenCost reported a zero cost for a running pod with non-zero requests"
    return None


def _parse_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _allocation_to_cost_metric(resource_id: str, item: Any, timestamp: datetime) -> Optional[CostMetric]:
    """Convert one OpenCost allocation into a pod ``CostMetric`` (``None`` for idle/unallocated entries)."""
    if not isinstance(item, dict):
        return None
    properties = item.get("properties") or {}
    namespace = properties.get("namespace")
    pod_name = properties.get("pod")
    if (not namespace or not pod_name) and "/" in resource_id:
        namespace, _, pod_name = resource_id.partition("/")
    if not pod_name:
        pod_name = resource_id
    if not namespace:
        logger.warning("Skipping metric for '%s' because namespace is missing.", pod_name)
        return None
    if namespace.startswith("__") or pod_name.startswith("__"):
        # __idle__, __unallocated__ and __unmounted__ are not pods.
        return None

    cpu = _number(item.get("cpuCost")) + _number(item.get("cpuCostAdjustment"))
    ram = _number(item.get("ramCost")) + _number(item.get("ramCostAdjustment"))
    storage = _number(item.get("pvCost")) + _number(item.get("pvCostAdjustment"))
    network = _number(item.get("networkCost")) + _number(item.get("networkCostAdjustment"))
    lb = _number(item.get("loadBalancerCost")) + _number(item.get("loadBalancerCostAdjustment"))
    gpu = _number(item.get("gpuCost")) + _number(item.get("gpuCostAdjustment"))
    components = cpu + ram + storage + network + lb + gpu
    total = _number(item["totalCost"]) if item.get("totalCost") is not None else components

    window = item.get("window") if isinstance(item.get("window"), dict) else {}
    minutes = item.get("minutes")
    untrusted = untrusted_cost_reason(item)
    if untrusted is not None:
        logger.debug("Cost of %s/%s is not trusted: %s", namespace, pod_name, untrusted)
    return CostMetric(
        pod_name=pod_name,
        namespace=namespace,
        cpu_cost=cpu,
        ram_cost=ram,
        storage_cost=storage,
        network_cost=network,
        lb_cost=lb,
        gpu_cost=gpu,
        total_cost=total,
        timestamp=timestamp,
        window_start=_parse_time(window.get("start")),
        window_end=_parse_time(window.get("end")),
        minutes=_number(minutes) if minutes is not None else None,
        source=CostSource.NONE.value if untrusted is not None else CostSource.OPENCOST.value,
    )


def opencost_step_share(cost: CostMetric, step_seconds: float, window_seconds: float) -> float:
    """Return the fraction of an allocation that belongs to one ``step_seconds`` sample.

    OpenCost reports how many minutes the pod ran inside the queried window.
    Dividing by those running minutes (not by the whole window) keeps a pod
    created mid-window from being under-costed (BUG-41); the share of a
    window that is a single step is 1.
    """
    running_seconds: Optional[float] = None
    if cost.minutes is not None and cost.minutes > 0:
        running_seconds = cost.minutes * 60.0
    elif cost.window_start is not None and cost.window_end is not None:
        running_seconds = (cost.window_end - cost.window_start).total_seconds()
    if not running_seconds or running_seconds <= 0:
        running_seconds = window_seconds
    if not running_seconds or running_seconds <= 0 or step_seconds <= 0:
        return 1.0
    return min(1.0, step_seconds / running_seconds)


class OpenCostCollector(BaseCollector):
    """
    Collects cost allocation data from an OpenCost service via an Ingress.
    The endpoint URL is read from application configuration (`config.OPENCOST_API_URL`).
    If not configured, the collector will attempt to discover the endpoint automatically.
    """

    def __init__(self):
        # Store the resolved URL in an instance variable to avoid modifying global config
        self._resolved_url: Optional[str] = None
        # Reusable HTTP client (lazily initialized)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self, verify: bool = True) -> httpx.AsyncClient:
        """Return the reusable HTTP client, creating it lazily if needed."""
        if self._client is None or self._client.is_closed:
            self._client = get_async_http_client(verify=verify)
        return self._client

    async def close(self):
        """Close the reusable HTTP client to release connection pool resources."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def collect(
        self,
        window: str = "1d",
        timestamp: Optional[datetime] = None,
        *,
        accumulate: bool = False,
    ) -> List[CostMetric]:
        """
        Fetches pod cost allocations from OpenCost ``/allocation/compute``.

        Allocations are aggregated by ``namespace,pod`` so same-named pods in
        different namespaces never collide (BUG-04), and idle cost is excluded
        (``includeIdle=false``; idle is allocated separately, see roadmap B2).

        Args:
            window: OpenCost window (duration such as ``1d`` or ``start,end`` unix timestamps).
            timestamp: Timestamp stamped on the returned metrics (defaults to now).
            accumulate: Sum the whole window into one allocation set.

        Returns:
            A list of CostMetric objects, or an empty list if an error occurs.
        """
        logger.info("Collecting data from OpenCostCollector (using Ingress)...")

        params = {"window": window, "aggregate": "namespace,pod"}
        if accumulate:
            params["accumulate"] = "true"
        params["includeIdle"] = "false"
        verify_certs = config.OPENCOST_VERIFY_CERTS

        client = await self._get_client(verify=verify_certs)
        # Resolve URL (Config -> Discovery -> Local -> In-Cluster)
        url = await self._resolve_url(client)

        if not url:
            logger.error("OpenCost API URL is not configured and discovery failed; skipping OpenCost collection")
            record_collection_request("opencost", "skipped")
            return []

        timer = start_timer()
        errors: list[str] = []

        async def _fetch(u: str):
            try:
                resp = await client.get(u, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.debug("Request to %s failed: %s", u, exc)
                errors.append(u)
                return None

            try:
                return resp.json().get("data")
            except Exception:
                # httpx doesn't separate json decode easily, usually ValueError? No, json.JSONDecodeError
                logger.error("Failed to decode JSON from %s. Server sent non-JSON response.", u)
                logger.debug("Raw response content from %s: %s", u, resp.text[:500])
                errors.append(u)
                return None

        response_data = await _fetch(url)

        # If the base url returned no usable data, try appending the well-known
        # allocation path used by some OpenCost deployments.
        if not response_data and not url.rstrip("/").endswith("/allocation/compute"):
            alt_path = url.rstrip("/") + "/allocation/compute"
            logger.debug("Trying alternative OpenCost path: %s", alt_path)
            response_data = await _fetch(alt_path)
            if response_data:
                # Prefer the alt path for future calls (stored in instance, not global config)
                self._resolved_url = alt_path

        if not response_data or not isinstance(response_data, list) or len(response_data) == 0:
            logger.warning(
                "OpenCost API returned no data. This can happen if the cluster is new or the endpoint is different."
            )
            record_collection_request("opencost", "error" if errors else "empty", elapsed(timer))
            return []

        now = timestamp if timestamp else datetime.now(timezone.utc)
        collected_metrics = []
        for allocation_set in response_data:
            if not isinstance(allocation_set, dict):
                continue
            for resource_id, item in allocation_set.items():
                metric = _allocation_to_cost_metric(str(resource_id), item, now)
                if metric is not None:
                    collected_metrics.append(metric)

        record_collection_request("opencost", "success" if collected_metrics else "empty", elapsed(timer))
        logger.info("Successfully collected %d metrics from OpenCost.", len(collected_metrics))
        return collected_metrics

    async def collect_pv_costs(self, pv_names: Sequence[str], window_days: int = 7) -> Dict[str, float]:
        """Fetch the storage cost of specific PersistentVolumes from OpenCost.

        Queries the OpenCost allocation API aggregated by ``persistentvolume``
        over the given window and extracts the per-volume storage cost.

        Args:
            pv_names: Names of the PersistentVolumes to look up.
            window_days: Observation window in days used for the allocation query.

        Returns:
            A mapping of ``{pv_name: window_cost_usd}`` for volumes OpenCost has
            cost data for. Returns an empty dict when OpenCost is unreachable,
            unconfigured, or has no cost data for the requested volumes.
        """
        if not pv_names:
            return {}

        verify_certs = config.OPENCOST_VERIFY_CERTS
        client = await self._get_client(verify=verify_certs)
        url = await self._resolve_url(client)

        if not url:
            logger.warning("OpenCost API URL is not configured and discovery failed; cannot fetch real PV costs.")
            return {}

        base = url.rstrip("/")
        if base.endswith("/allocation/compute"):
            base = base[: -len("/allocation/compute")]
        params = {"aggregateBy": "persistentvolume", "window": f"{window_days}d"}

        async def _fetch(u: str) -> Optional[dict]:
            try:
                resp = await client.get(u, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.debug("OpenCost PV cost request to %s failed: %s", u, exc)
                return None
            try:
                payload = resp.json()
                data = payload.get("data")
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    return data[0]
            except Exception:
                logger.debug("Failed to decode OpenCost PV cost response from %s", u)
            return None

        # OpenCost deployments expose the allocation endpoint under different
        # paths; try each until one responds.
        for candidate in (
            base + "/api/v1/allocation/aggregate",
            base + "/allocation/aggregate",
            base + "/allocation",
        ):
            allocations = await _fetch(candidate)
            if allocations is not None:
                break
        else:
            allocations = None

        if not allocations:
            return {}

        costs: Dict[str, float] = {}
        for name in pv_names:
            entry = allocations.get(name)
            if entry is None:
                entry = next(
                    (a for a in allocations.values() if (a.get("properties") or {}).get("persistentVolume") == name),
                    None,
                )
            if not entry:
                continue
            window_cost = (entry.get("pvCosts") or {}).get(name)
            if window_cost is None:
                window_cost = entry.get("storageCost")
            if window_cost:
                costs[name] = float(window_cost)

        if costs:
            logger.info("Retrieved real OpenCost storage costs for %d PersistentVolumes.", len(costs))
        return costs

    async def collect_lb_costs(self, service_keys: Sequence[str], window_days: int = 7) -> Dict[str, float]:
        """Fetch the LoadBalancer cost of specific Services from OpenCost.

        Queries the OpenCost allocation API aggregated by ``service`` over the
        given window and extracts the per-service ``loadBalancerCosts`` map.

        Args:
            service_keys: Keys of the Services to look up, formatted as
                          ``namespace/name``.
            window_days: Observation window in days used for the allocation query.

        Returns:
            A mapping of ``{service_key: window_cost_usd}`` for Services OpenCost
            has LoadBalancer cost data for. Returns an empty dict when OpenCost
            is unreachable, unconfigured, or has no cost data for the requested
            Services.
        """
        if not service_keys:
            return {}

        verify_certs = config.OPENCOST_VERIFY_CERTS
        client = await self._get_client(verify=verify_certs)
        url = await self._resolve_url(client)

        if not url:
            logger.warning("OpenCost API URL is not configured and discovery failed; cannot fetch real LB costs.")
            return {}

        base = url.rstrip("/")
        if base.endswith("/allocation/compute"):
            base = base[: -len("/allocation/compute")]
        params = {"aggregateBy": "service", "window": f"{window_days}d"}

        async def _fetch(u: str) -> Optional[dict]:
            try:
                resp = await client.get(u, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.debug("OpenCost LB cost request to %s failed: %s", u, exc)
                return None
            try:
                payload = resp.json()
                data = payload.get("data")
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    return data[0]
            except Exception:
                logger.debug("Failed to decode OpenCost LB cost response from %s", u)
            return None

        # OpenCost deployments expose the allocation endpoint under different
        # paths; try each until one responds.
        for candidate in (
            base + "/api/v1/allocation/aggregate",
            base + "/allocation/aggregate",
            base + "/allocation",
        ):
            allocations = await _fetch(candidate)
            if allocations is not None:
                break
        else:
            allocations = None

        if not allocations:
            return {}

        costs: Dict[str, float] = {}
        wanted = set(service_keys)
        for entry in allocations.values():
            if not isinstance(entry, dict):
                continue
            lb_costs = entry.get("loadBalancerCosts") or {}
            if not isinstance(lb_costs, dict):
                continue
            for key, window_cost in lb_costs.items():
                key = str(key)
                if not window_cost:
                    continue
                # OpenCost keys LB costs by "namespace/service-name". Match
                # requested keys directly or by the service name suffix.
                if key in wanted:
                    costs[key] = float(window_cost)
                else:
                    service_name = key.rsplit("/", 1)[-1]
                    matches = [k for k in wanted if k.rsplit("/", 1)[-1] == service_name]
                    for match in matches:
                        costs[match] = float(window_cost)

        if costs:
            logger.info("Retrieved real OpenCost LoadBalancer costs for %d Services.", len(costs))
        return costs

    async def collect_range(self, start: datetime, end: datetime) -> List[CostMetric]:
        """Collect the cost allocated to each pod over exactly ``[start, end)``.

        The window is queried as-is with ``accumulate=true`` (no trailing
        ``1d`` divided by steps, BUG-41). Each metric carries its window and
        the minutes the pod ran in it, so callers can attribute a step's share
        with :func:`opencost_step_share`.
        """
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        window = f"{start_ts},{end_ts}"
        metrics = await self.collect(window=window, timestamp=end, accumulate=True)
        for metric in metrics:
            if metric.window_start is None:
                metric.window_start = start
            if metric.window_end is None:
                metric.window_end = end
        return metrics

    async def is_available(self) -> bool:
        """
        Quick probe to check if the OpenCost API URL is reachable and returns a
        2xx response. Returns True when reachable, False otherwise.
        """
        verify_certs = config.OPENCOST_VERIFY_CERTS
        client = await self._get_client(verify=verify_certs)
        # First check if we already have a resolved URL from a previous call
        url = self._resolved_url or getattr(config, "OPENCOST_API_URL", None)

        # If not configured, try discovery first
        if not url:
            try:
                od = OpenCostDiscovery()
                discovered = await od.discover()
                if discovered:
                    self._resolved_url = discovered
                    url = discovered
            except Exception:
                logger.debug("OpenCost discovery attempt failed or returned no candidate")

        if url and await self._probe(client, url):
            logger.debug("OpenCost API is available at %s", url)
            self._resolved_url = url
            return True

        # If configured URL didn't respond, try the well-known allocation path
        if url:
            alt = url.rstrip("/") + "/allocation/compute"
            if await self._probe(client, alt):
                logger.debug("OpenCost API is available at alternative path %s", alt)
                self._resolved_url = alt
                return True

        # try discovery as a fallback
        try:
            od = OpenCostDiscovery()
            discovered = await od.discover()
            if discovered and await self._probe(client, discovered):
                # Store in instance variable so subsequent calls use discovered URL
                self._resolved_url = discovered
                return True
        except Exception:
            logger.debug("OpenCost discovery/probe failed")

        logger.debug("OpenCost API is not available")
        return False

    async def _resolve_url(self, client: httpx.AsyncClient) -> Optional[str]:
        """
        Resolves the OpenCost API URL by checking instance cache, config, discovery, and fallback candidates.
        The resolved URL is stored in self._resolved_url for future calls.
        """
        # 0. Return cached URL if already resolved
        if self._resolved_url:
            return self._resolved_url

        # 1. Configured URL
        url = getattr(config, "OPENCOST_API_URL", None)
        if url:
            self._resolved_url = url
            return url

        # 2. Discovery
        try:
            od = OpenCostDiscovery()
            discovered = await od.discover()
            if discovered:
                self._resolved_url = discovered
                return discovered
        except Exception:
            logger.debug("OpenCost discovery failed")

        # 3. Localhost candidates
        candidates = [
            "http://localhost:9003",
            "http://opencost:9003",
            "http://localhost:9090",
        ]
        for candidate in candidates:
            if await self._probe(client, candidate):
                self._resolved_url = candidate
                return candidate

        # 4. In-cluster candidates
        bd = BaseDiscovery()
        if bd._is_running_in_cluster():
            cluster_candidates = [
                "http://opencost.opencost.svc.cluster.local:9003",
                "http://opencost.svc.cluster.local:9003",
                "http://opencost.opencost.svc.cluster.local:9090",
            ]
            for candidate in cluster_candidates:
                if await self._probe(client, candidate):
                    self._resolved_url = candidate
                    return candidate

        return None

    async def _probe(self, client: httpx.AsyncClient, url: str) -> bool:
        """
        Probes a URL to see if it's a valid OpenCost endpoint.
        """
        try:
            params = {"window": "1d", "aggregate": "pod"}
            r = await client.get(url, params=params)
            r.raise_for_status()
            return True
        except Exception:
            return False
