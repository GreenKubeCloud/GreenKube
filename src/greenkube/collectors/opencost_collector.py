import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx

# from requests.packages.urllib3.exceptions import InsecureRequestWarning
# Removing InsecureRequestWarning import if not used by httpx directly or handle differently.
# Httpx doesn't emit InsecureRequestWarning in same way usually, just ignore verify=False.
from greenkube.collectors.discovery.base import BaseDiscovery
from greenkube.collectors.discovery.opencost import OpenCostDiscovery

from ..core.config import config
from ..core.observability import (
    OPENCOST_AMBIGUOUS_CLUSTER,
    OPENCOST_AUTH_FAILED,
    OPENCOST_UNREBUILT_ALLOCATIONS,
    elapsed,
    record_collection_request,
    start_timer,
)
from ..models.metrics import CostMetric
from ..models.provenance import CostSource
from ..utils.http_client import get_async_http_client
from .base_collector import BaseCollector

logger = logging.getLogger(__name__)

#: OpenCost answered with allocations for the window.
OPENCOST_STATUS_OK = "ok"
#: OpenCost answered, but with no allocation for the window (lag, or a gap in its own data).
OPENCOST_STATUS_EMPTY = "empty"
#: No OpenCost URL is configured and none was discovered: the cluster has no OpenCost.
OPENCOST_STATUS_NOT_CONFIGURED = "not_configured"


def _of_other_cluster(item: Any) -> bool:
    """Whether an allocation names a cluster other than ``OPENCOST_CLUSTER_ID`` (COST-10 review 4).

    Older OpenCost and Kubecost releases ignore ``filter=``, so the filter is
    enforced on the answer too. An item without ``properties.cluster`` is kept.
    """
    cluster_id = getattr(config, "OPENCOST_CLUSTER_ID", None)
    if not cluster_id or not isinstance(item, dict):
        return False
    cluster = (item.get("properties") or {}).get("cluster")
    return bool(cluster) and str(cluster) != str(cluster_id)


def _own_cluster(allocation_set: Mapping[str, Any]) -> Dict[str, Any]:
    """``allocation_set`` without the allocations of other clusters (see :func:`_of_other_cluster`)."""
    return {key: item for key, item in allocation_set.items() if not _of_other_cluster(item)}


def _cluster_params() -> Dict[str, str]:
    """OpenCost filter on ``OPENCOST_CLUSTER_ID`` (COST-10); empty when no id is configured."""
    cluster_id = getattr(config, "OPENCOST_CLUSTER_ID", None)
    return {"filter": f'cluster:"{cluster_id}"'} if cluster_id else {}


class OpenCostUnavailableError(RuntimeError):
    """A configured or discovered OpenCost could not answer an allocation request (COST-01).

    Raised for transport errors, non-2xx answers (401, 503, ...) and invalid
    JSON, so callers never mistake an outage for a cluster without OpenCost.
    """


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


#: OpenCost writes allocation figures with 5 decimals: the smallest non-zero cost is 1e-5.
OPENCOST_PRECISION = 1e-5
#: Largest rounding error of one OpenCost figure.
_HALF_PRECISION = OPENCOST_PRECISION / 2
#: Below this cost a figure's rounding error exceeds 0.5% (a quarter of the
#: verifier's 2% noise floor): such allocations are rebuilt from resource-hours.
PRECISION_FLOOR = 1e-3
#: Relative tolerance between a rebuilt cost and OpenCost's figure, on top of rounding.
_REBUILD_TOLERANCE = 0.02
#: Look-back of the node-level allocation that yields OpenCost's node unit prices.
NODE_PRICE_LOOKBACK_SECONDS = 24 * 3600
#: Node unit prices are re-read once per hour of collection time.
_NODE_PRICE_CACHE_SECONDS = 3600
_OTHER_COST_KEYS = (
    "pvCost",
    "pvCostAdjustment",
    "networkCost",
    "networkCostAdjustment",
    "loadBalancerCost",
    "loadBalancerCostAdjustment",
    "gpuCost",
    "gpuCostAdjustment",
)


@dataclass(frozen=True)
class NodeUnitPrices:
    """OpenCost's own unit prices on one node (``None`` when not known precisely)."""

    cpu_core_hour: Optional[float]
    ram_byte_hour: Optional[float]


def _precise_rate(cost: Any, quantity: Any) -> Optional[float]:
    cost_value = _optional_number(cost)
    quantity_value = _optional_number(quantity)
    if cost_value is None or quantity_value is None or quantity_value <= 0 or cost_value < PRECISION_FLOOR:
        return None
    return cost_value / quantity_value


def node_unit_prices(allocations: Mapping[str, Any]) -> Dict[str, NodeUnitPrices]:
    """OpenCost unit prices per node from an ``aggregate=node`` allocation set.

    OpenCost prices a pod's CPU and RAM as its core-hours and byte-hours times
    its node's hourly prices, so a node's ``cpuCost / cpuCoreHours`` and
    ``ramCost / ramByteHours`` are those prices. A price is kept only when the
    node's cost is large enough (``PRECISION_FLOOR``) for OpenCost's rounding
    to be negligible; a node without a price (cost 0) gets none.
    """
    prices: Dict[str, NodeUnitPrices] = {}
    for key, item in allocations.items():
        if not isinstance(item, dict):
            continue
        node = (item.get("properties") or {}).get("node")
        if not node and key and not str(key).startswith("__"):
            node = key
        if not node:
            continue
        unit = NodeUnitPrices(
            cpu_core_hour=_precise_rate(item.get("cpuCost"), item.get("cpuCoreHours")),
            ram_byte_hour=_precise_rate(item.get("ramCost"), item.get("ramByteHours")),
        )
        if unit.cpu_core_hour is not None or unit.ram_byte_hour is not None:
            prices[str(node)] = unit
    return prices


def broken_cost_reason(item: dict) -> Optional[str]:
    """Why an OpenCost allocation's cost is broken (``None`` when it is a finite figure).

    A missing cost is missing, never a zero (fail closed): ``totalCost``,
    ``cpuCost`` or ``ramCost`` present but NaN, infinite or not a number, and
    a total that is neither reported nor derivable from the CPU and RAM costs.
    """
    for key in ("totalCost", "cpuCost", "ramCost"):
        if key in item and item[key] is not None and _optional_number(item[key]) is None:
            return f"OpenCost {key} is not a finite number ({item[key]!r})"
    total = _optional_number(item.get("totalCost"))
    if total is None and (
        _optional_number(item.get("cpuCost")) is None or _optional_number(item.get("ramCost")) is None
    ):
        return "OpenCost reported no total, CPU or RAM cost"
    return None


def _ran_with_requests(item: dict) -> bool:
    minutes = _optional_number(item.get("minutes"))
    requested = any(
        (_optional_number(item.get(key)) or 0.0) > 0 for key in ("cpuCoreRequestAverage", "ramByteRequestAverage")
    )
    return minutes is not None and minutes > 0 and requested


def untrusted_cost_reason(item: dict) -> Optional[str]:
    """Why an OpenCost allocation's own figures must not back a measurement (``None`` when they can).

    Broken costs (:func:`broken_cost_reason`) and a zero total for a pod that
    ran (``minutes > 0``) with non-zero requests (OpenCost had no price for
    it) are untrusted. Rows built from such an allocation carry
    ``cost_source = none``, so a NaN never becomes a measured saving. A zero
    that is only OpenCost's 5-decimal rounding is rescued by
    :func:`rebuilt_cost` before this rule applies.
    """
    broken = broken_cost_reason(item)
    if broken is not None:
        return broken
    total = _optional_number(item.get("totalCost"))
    if total is not None and total == 0 and _ran_with_requests(item):
        return "OpenCost reported a zero cost for a running pod with non-zero requests"
    return None


def _ran_or_requested(item: dict) -> bool:
    """Whether the allocation shows a positive request or usage average (the pod was there)."""
    return any(
        (_optional_number(item.get(key)) or 0.0) > 0
        for key in ("cpuCoreRequestAverage", "ramByteRequestAverage", "cpuCoreUsageAverage", "ramByteUsageAverage")
    )


def unusable_cost_reason(item: dict) -> Optional[str]:
    """Why an allocation that is not rebuilt carries no usable cost (``None`` when its figures can be used).

    COST-05: an allocation with no running time (``minutes <= 0`` or a window
    whose start equals its end, OpenCost's answer after a gap in its own data)
    carries no cost whatever its requests. A non-zero figure below
    ``PRECISION_FLOOR`` that :func:`rebuilt_cost` could not rebuild (node price
    unknown, figures disagree) has a rounding error unbounded relative to its
    value. A zero for a pod with a request or usage is a missing price. Only a
    zero for a pod that ran without any request or usage is a usable zero.
    """
    minutes = _optional_number(item.get("minutes"))
    window = item.get("window") if isinstance(item.get("window"), dict) else {}
    start, end = _parse_time(window.get("start")), _parse_time(window.get("end"))
    if (minutes is not None and minutes <= 0) or (start is not None and start == end):
        return "OpenCost allocation has no running time (minutes = 0)"
    total = _reported_total(item)
    if 0 < total < PRECISION_FLOOR:
        return f"OpenCost cost {total:g} is below its precision and could not be rebuilt from node prices"
    if total == 0 and _ran_or_requested(item):
        return "OpenCost reported a zero cost for a pod with requests or usage"
    return None


def _core_hours(item: dict, hours: float) -> Optional[float]:
    """The allocation's core-hours, refined within the rounding of ``cpuCoreHours``.

    OpenCost charges ``max(request, usage)`` per minute. For a sub-precision
    value the averages times the runtime are more precise; they are clamped to
    the interval the rounded ``cpuCoreHours`` allows.
    """
    reported = _optional_number(item.get("cpuCoreHours"))
    if reported is None or reported < 0:
        return None
    averaged = max(
        _optional_number(item.get("cpuCoreRequestAverage")) or 0.0,
        _optional_number(item.get("cpuCoreUsageAverage")) or 0.0,
    )
    if averaged <= 0:
        return reported
    return max(0.0, min(max(averaged * hours, reported - _HALF_PRECISION), reported + _HALF_PRECISION))


def _agrees(reported: Optional[float], rebuilt: float, rounded_terms: int = 1) -> bool:
    """Whether OpenCost's rounded figure is the rebuilt cost, within its rounding and tolerance."""
    if reported is None:
        return True
    tolerance = rounded_terms * _HALF_PRECISION * 1.01 + _REBUILD_TOLERANCE * rebuilt
    return abs(reported - rebuilt) <= tolerance


def _reported_total(item: dict) -> float:
    total = _optional_number(item.get("totalCost"))
    if total is not None:
        return total
    return (
        _number(item.get("cpuCost"))
        + _number(item.get("ramCost"))
        + sum(_number(item.get(k)) for k in _OTHER_COST_KEYS)
    )


def needs_node_prices(item: Any) -> bool:
    """Whether an allocation may be below OpenCost's precision (worth the node price query)."""
    if not isinstance(item, dict) or broken_cost_reason(item) is not None:
        return False
    if not (item.get("properties") or {}).get("node"):
        return False
    minutes = _optional_number(item.get("minutes"))
    if minutes is None or minutes <= 0:
        return False
    return 0 <= _reported_total(item) < PRECISION_FLOOR


def rebuilt_cost(item: dict, prices: Optional[NodeUnitPrices]) -> Optional[tuple[float, float]]:
    """``(cpu, ram)`` cost of a sub-precision allocation rebuilt at OpenCost's node prices.

    OpenCost prices CPU and RAM as core-hours and byte-hours times the node's
    hourly prices, then writes 5 decimals: a pod costing less than 5e-6 per
    window reads 0. The cost is rebuilt as the same product with the node's
    OpenCost prices (:func:`node_unit_prices`). ``None`` unless the allocation
    is finite and non-negative, ran, costs less than ``PRECISION_FLOOR``, and
    OpenCost's own CPU, RAM and total figures equal the rebuilt costs within
    their rounding. A zero that rounding cannot explain (a priced pod reported
    at 0, a node without a price) is not rebuilt and stays untrusted.
    """
    if prices is None or not needs_node_prices(item):
        return None
    reported_cpu = _optional_number(item.get("cpuCost"))
    reported_ram = _optional_number(item.get("ramCost"))
    if (reported_cpu or 0.0) < 0 or (reported_ram or 0.0) < 0:
        return None
    minutes = _optional_number(item.get("minutes")) or 0.0
    core_hours = _core_hours(item, minutes / 60.0)
    byte_hours = _optional_number(item.get("ramByteHours"))
    if core_hours is None or byte_hours is None or byte_hours < 0:
        return None
    if (core_hours > 0 and prices.cpu_core_hour is None) or (byte_hours > 0 and prices.ram_byte_hour is None):
        return None
    cpu = core_hours * (prices.cpu_core_hour or 0.0)
    ram = byte_hours * (prices.ram_byte_hour or 0.0)
    if not (_agrees(reported_cpu, cpu) and _agrees(reported_ram, ram)):
        return None
    adjustments = _number(item.get("cpuCostAdjustment")) + _number(item.get("ramCostAdjustment"))
    others = sum(_number(item.get(key)) for key in _OTHER_COST_KEYS)
    if not _agrees(_optional_number(item.get("totalCost")), cpu + ram + adjustments + others, rounded_terms=3):
        return None
    return cpu + _number(item.get("cpuCostAdjustment")), ram + _number(item.get("ramCostAdjustment"))


def _parse_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _allocation_to_cost_metric(
    resource_id: str,
    item: Any,
    timestamp: datetime,
    node_prices: Optional[Mapping[str, NodeUnitPrices]] = None,
) -> Optional[CostMetric]:
    """Convert one OpenCost allocation into a pod ``CostMetric`` (``None`` for idle/unallocated entries).

    An allocation below OpenCost's precision whose figures agree with its
    resource-hours at the node's OpenCost unit prices (``node_prices``) gets
    the rebuilt cost and ``source = opencost_derived`` (:func:`rebuilt_cost`).
    """
    if not isinstance(item, dict):
        return None
    properties = item.get("properties") or {}
    namespace = properties.get("namespace")
    pod_name = properties.get("pod")
    if (not namespace or not pod_name) and "/" in resource_id:
        # Keys are "<namespace>/<pod>", or "<cluster>/<namespace>/<pod>" with aggregate=cluster,namespace,pod.
        namespace, pod_name = resource_id.split("/")[-2:]
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
    node = (item.get("properties") or {}).get("node")
    rebuilt = rebuilt_cost(item, (node_prices or {}).get(node) if node else None)
    if rebuilt is not None:
        source = CostSource.OPENCOST_DERIVED.value
        cpu, ram = rebuilt
        total = cpu + ram + storage + network + lb + gpu
    else:
        untrusted = untrusted_cost_reason(item)
        if untrusted is None:
            untrusted = unusable_cost_reason(item)
            if untrusted is not None:
                OPENCOST_UNREBUILT_ALLOCATIONS.inc()
        if untrusted is not None:
            logger.debug("Cost of %s/%s is not trusted: %s", namespace, pod_name, untrusted)
        source = CostSource.NONE.value if untrusted is not None else CostSource.OPENCOST.value
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
        running_start=_parse_time(item.get("start")),
        running_end=_parse_time(item.get("end")),
        source=source,
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


def _running_interval(cost: CostMetric) -> Optional[tuple[datetime, datetime]]:
    """The pod's running interval inside the allocation window (``None`` when OpenCost did not report it)."""
    if cost.running_start is None or cost.running_end is None:
        return None
    start, end = cost.running_start, cost.running_end
    if cost.window_start is not None:
        start = max(start, cost.window_start)
    if cost.window_end is not None:
        end = min(end, cost.window_end)
    return (start, end) if end > start else None


def opencost_step_shares(cost: CostMetric, step_starts: Sequence[datetime], step: timedelta) -> Dict[datetime, float]:
    """Fraction of a window allocation that belongs to each step ``[s, s + step)`` of ``step_starts`` (COL-10).

    The share of a step is the pod's running seconds inside the step divided
    by its running seconds in the window (OpenCost's ``start`` and ``end``),
    so the shares of all the steps the pod ran in sum to 1, whatever the
    number of rows and however the window was chunked: a pod that ran 3 of
    its 7 minutes in a step gets 3/7 of the cost there. A step the pod did
    not run in gets 0; the share of a running step without a row stays out
    of the stored rows and shows in the cost reconciliation (COL-03). Without
    a running interval the former rule applies (:func:`opencost_step_share`).
    """
    interval = _running_interval(cost)
    if interval is None:
        window_seconds = (
            (cost.window_end - cost.window_start).total_seconds()
            if cost.window_start is not None and cost.window_end is not None
            else 0.0
        )
        share = opencost_step_share(cost, step.total_seconds(), window_seconds)
        return {start: share for start in step_starts}
    start, end = interval
    running = (end - start).total_seconds()
    shares: Dict[datetime, float] = {}
    for step_start in step_starts:
        overlap = (min(end, step_start + step) - max(start, step_start)).total_seconds()
        shares[step_start] = max(overlap, 0.0) / running
    return shares


@dataclass(frozen=True)
class AssetCost:
    """OpenCost cost of a PersistentVolume or LoadBalancer over a window (COST-03).

    ``quantity_hours`` is the volume's ``byteHours`` or the load balancer's
    ``hours``: the caller annualizes over the time the asset existed.
    """

    cost: float
    quantity_hours: Optional[float] = None


#: Hours per year used to annualize an asset cost.
HOURS_PER_YEAR = 8760.0


def annualize_asset_cost(cost: float, hours: Optional[float], window_days: int) -> float:
    """Annual cost of an asset from its cost over ``hours`` of existence (``window_days`` when unknown)."""
    if cost == 0:
        return 0.0
    if hours is not None and hours > 0:
        return cost / hours * HOURS_PER_YEAR
    return cost * (365.0 / window_days)


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
        # OpenCost node unit prices with the hour of collection time they were read for.
        self._node_prices: Optional[tuple[int, Dict[str, NodeUnitPrices]]] = None
        #: Outcome of the last allocation request (``OPENCOST_STATUS_*``; ``None`` after an error).
        self.last_status: Optional[str] = None

    async def _get_client(self, verify: bool = True) -> httpx.AsyncClient:
        """Return the reusable HTTP client, creating it lazily if needed."""
        if self._client is None or self._client.is_closed:
            self._client = get_async_http_client(verify=verify)
        return self._client

    @staticmethod
    def _auth_kwargs() -> Dict[str, Any]:
        """Credentials of every OpenCost request (COST-06): bearer token first, else basic auth, else none.

        Same precedence as the discovery probe. Read at each request, so a
        changed setting applies at once; ``SafeAsyncClient`` drops them on a
        cross-host redirect.
        """
        token = getattr(config, "OPENCOST_BEARER_TOKEN", None)
        if token:
            return {"headers": {"Authorization": f"Bearer {token}"}}
        username = getattr(config, "OPENCOST_USERNAME", None)
        password = getattr(config, "OPENCOST_PASSWORD", None)
        if username and password:
            return {"auth": httpx.BasicAuth(username, password)}
        return {}

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
            The pod CostMetric objects. An empty list means OpenCost answered
            without allocations (``last_status = empty``) or no OpenCost is
            configured or discoverable (``last_status = not_configured``).

        Raises:
            OpenCostUnavailableError: the resolved OpenCost failed (transport
                error, non-2xx answer, invalid JSON); never reported as ``[]``.
        """
        self.last_status = None
        logger.info("Collecting data from OpenCostCollector (using Ingress)...")

        # Aggregated by cluster too, so a multi-cluster OpenCost or Kubecost never sums same-named
        # pods of different clusters; OPENCOST_CLUSTER_ID filters the query (COST-10).
        params = {"window": window, "aggregate": "cluster,namespace,pod", **_cluster_params()}
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
            self.last_status = OPENCOST_STATUS_NOT_CONFIGURED
            return []

        timer = start_timer()
        errors: list[str] = []
        answered: list[str] = []

        async def _fetch(u: str):
            try:
                resp = await client.get(u, params=params, **self._auth_kwargs())
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    # A configuration error, not an outage: say so loudly (COST-06).
                    OPENCOST_AUTH_FAILED.set(1)
                    logger.error(
                        "OpenCost rejected the allocation request with HTTP %d: check OPENCOST_BEARER_TOKEN or "
                        "OPENCOST_USERNAME and OPENCOST_PASSWORD; the window's cost stays opencost_pending.",
                        exc.response.status_code,
                    )
                else:
                    logger.debug("Request to %s failed: %s", u, exc)
                errors.append(f"{u}: {exc}")
                return None
            except httpx.HTTPError as exc:
                logger.debug("Request to %s failed: %s", u, exc)
                errors.append(f"{u}: {exc}")
                return None
            OPENCOST_AUTH_FAILED.set(0)

            try:
                data = resp.json().get("data")
            except Exception:
                # httpx doesn't separate json decode easily, usually ValueError? No, json.JSONDecodeError
                logger.error("Failed to decode JSON from %s. Server sent non-JSON response.", u)
                logger.debug("Raw response content from %s: %s", u, resp.text[:500])
                errors.append(f"{u}: invalid JSON")
                return None
            if isinstance(data, list):
                answered.append(u)
            else:
                errors.append(f"{u}: no allocation data in the answer")
            return data

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

        if not answered:
            record_collection_request("opencost", "error", elapsed(timer))
            raise OpenCostUnavailableError("OpenCost allocation request failed: " + "; ".join(errors))
        if not response_data or not isinstance(response_data, list) or len(response_data) == 0:
            logger.warning(
                "OpenCost API returned no data. This can happen if the cluster is new or the endpoint is different."
            )
            record_collection_request("opencost", "empty", elapsed(timer))
            self.last_status = OPENCOST_STATUS_EMPTY
            return []

        now = timestamp if timestamp else datetime.now(timezone.utc)
        allocation_sets = [
            _own_cluster(allocation_set) for allocation_set in response_data if isinstance(allocation_set, dict)
        ]
        clusters = {
            str((item.get("properties") or {}).get("cluster"))
            for allocation_set in allocation_sets
            for item in allocation_set.values()
            if isinstance(item, dict) and (item.get("properties") or {}).get("cluster")
        }
        if len(clusters) > 1 and not _cluster_params():
            OPENCOST_AMBIGUOUS_CLUSTER.set(1)
            record_collection_request("opencost", "error", elapsed(timer))
            logger.error(
                "OpenCost answered for several clusters (%s) and OPENCOST_CLUSTER_ID is not set; no OpenCost cost "
                "is stored rather than merging same-named pods of different clusters.",
                ", ".join(sorted(clusters)),
            )
            raise OpenCostUnavailableError(
                "OpenCost serves several clusters; set OPENCOST_CLUSTER_ID (config.opencost.clusterId)"
            )
        OPENCOST_AMBIGUOUS_CLUSTER.set(0)
        node_prices: Dict[str, NodeUnitPrices] = {}
        nodes = {
            str(item["properties"]["node"])
            for allocation_set in allocation_sets
            for item in allocation_set.values()
            if needs_node_prices(item)
        }
        if nodes:
            node_prices = await self._fetch_node_prices(client, self._resolved_url or url, now, nodes)
        collected_metrics = []
        for allocation_set in allocation_sets:
            for resource_id, item in allocation_set.items():
                metric = _allocation_to_cost_metric(str(resource_id), item, now, node_prices)
                if metric is not None:
                    collected_metrics.append(metric)

        record_collection_request("opencost", "success" if collected_metrics else "empty", elapsed(timer))
        self.last_status = OPENCOST_STATUS_OK if collected_metrics else OPENCOST_STATUS_EMPTY
        logger.info("Successfully collected %d metrics from OpenCost.", len(collected_metrics))
        return collected_metrics

    async def _fetch_node_prices(
        self, client: httpx.AsyncClient, url: str, end: datetime, nodes: set[str]
    ) -> Dict[str, NodeUnitPrices]:
        """OpenCost's unit prices per node over the day before ``end``.

        One ``aggregate=node`` query: a node's cost over a day is large enough
        for OpenCost's 5-decimal rounding to be negligible. The result is
        reused within the same hour of collection time while it covers every
        node in ``nodes`` (a fresh cluster or a new node is re-read). Any
        failure yields no prices, so sub-precision zeros stay untrusted (fail
        closed).
        """
        end_ts = int(end.timestamp())
        bucket = end_ts // _NODE_PRICE_CACHE_SECONDS
        if self._node_prices is not None and self._node_prices[0] == bucket and nodes <= set(self._node_prices[1]):
            return self._node_prices[1]
        params = {
            "window": f"{end_ts - NODE_PRICE_LOOKBACK_SECONDS},{end_ts}",
            "aggregate": "node",
            "accumulate": "true",
            "includeIdle": "false",
            **_cluster_params(),
        }
        prices: Dict[str, NodeUnitPrices] = {}
        try:
            resp = await client.get(url, params=params, **self._auth_kwargs())
            resp.raise_for_status()
            data = resp.json().get("data")
        except Exception as exc:
            logger.warning("Could not read OpenCost node unit prices; sub-precision costs stay untrusted: %s", exc)
            return {}
        if not isinstance(data, list):
            logger.warning("OpenCost returned no node allocation; sub-precision costs stay untrusted.")
            return {}
        for allocation_set in data:
            if isinstance(allocation_set, dict):
                prices.update(node_unit_prices(_own_cluster(allocation_set)))
        self._node_prices = (bucket, prices)
        return prices

    async def _asset_allocations(self, window_days: int, what: str) -> Optional[List[dict]]:
        """Allocations of the last ``window_days`` with their ``pvs`` and ``lbAllocations`` maps (COST-03).

        One request to the allocation endpoint ``collect`` uses
        (``/allocation/compute``) with ``aggregate=namespace``,
        ``accumulate=true`` and ``includeIdle=false``. ``None`` when OpenCost is
        not configured or the request fails.
        """
        client = await self._get_client(verify=config.OPENCOST_VERIFY_CERTS)
        url = await self._resolve_url(client)
        if not url:
            logger.warning("OpenCost API URL is not configured and discovery failed; cannot fetch real %s costs.", what)
            return None
        base = url.rstrip("/")
        endpoint = base if base.endswith("/allocation/compute") else base + "/allocation/compute"
        end_ts = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
        params = {
            "window": f"{end_ts - int(window_days) * 86400},{end_ts}",
            "aggregate": "namespace",
            "accumulate": "true",
            "includeIdle": "false",
            **_cluster_params(),
        }
        try:
            resp = await client.get(endpoint, params=params, **self._auth_kwargs())
            resp.raise_for_status()
            data = resp.json().get("data")
        except Exception as exc:
            logger.warning("Could not read %s costs from OpenCost at %s: %s", what, endpoint, exc)
            return None
        if not isinstance(data, list):
            logger.warning("OpenCost returned no allocation data for %s costs.", what)
            return None
        return [
            item
            for allocation_set in data
            if isinstance(allocation_set, dict)
            for item in allocation_set.values()
            if isinstance(item, dict) and not _of_other_cluster(item)
        ]

    async def collect_pv_cost_details(self, pv_names: Sequence[str], window_days: int = 7) -> Dict[str, AssetCost]:
        """Cost and byte-hours of specific PersistentVolumes over the last ``window_days`` (COST-03).

        OpenCost reports a volume's cost in each allocation's ``pvs`` map, keyed
        ``cluster=<id>:name=<pv>`` with ``cost``, ``adjustment`` and
        ``byteHours``; every allocation is read, ``__unmounted__`` included, and
        a volume shared by several allocations is summed. A volume OpenCost
        knows at cost 0 is returned with 0.0 (an observed zero); a volume it
        does not know is absent.
        """
        if not pv_names:
            return {}
        allocations = await self._asset_allocations(window_days, "PersistentVolume")
        if not allocations:
            return {}
        wanted = set(pv_names)
        costs: Dict[str, AssetCost] = {}
        for item in allocations:
            pvs = item.get("pvs")
            if not isinstance(pvs, dict):
                continue
            for key, entry in pvs.items():
                name = str(key).rpartition(":name=")[2] if ":name=" in str(key) else str(key)
                if name not in wanted or not isinstance(entry, dict):
                    continue
                cost = _optional_number(entry.get("cost"))
                if cost is None:
                    continue
                previous = costs.get(name, AssetCost(0.0, 0.0))
                costs[name] = AssetCost(
                    cost=previous.cost + cost + _number(entry.get("adjustment")),
                    quantity_hours=(previous.quantity_hours or 0.0) + _number(entry.get("byteHours")),
                )
        if costs:
            logger.info("Retrieved OpenCost storage costs for %d PersistentVolumes.", len(costs))
        return costs

    async def collect_pv_costs(self, pv_names: Sequence[str], window_days: int = 7) -> Dict[str, float]:
        """Cost of specific PersistentVolumes over the last ``window_days`` (see :meth:`collect_pv_cost_details`).

        Returns ``{pv_name: window_cost}``; an observed zero is kept as 0.0, and
        an empty dict means OpenCost is unreachable, unconfigured, or does not
        know the volumes.
        """
        details = await self.collect_pv_cost_details(pv_names, window_days)
        return {name: detail.cost for name, detail in details.items()}

    async def collect_lb_cost_details(self, service_keys: Sequence[str], window_days: int = 7) -> Dict[str, AssetCost]:
        """Cost and hours of specific LoadBalancer Services over the last ``window_days`` (COST-03).

        OpenCost reports LoadBalancer cost in each allocation's
        ``lbAllocations`` map, keyed ``<cluster>/<namespace>/<service>`` with
        ``service`` (``namespace/name``), ``cost``, ``adjustment`` and
        ``hours``. A Service matches on its exact ``namespace/name`` only, so
        a same-named Service of another namespace is never attached. Its cost is
        summed over the allocations that share it; its hours are the largest
        reported. An observed zero is kept.
        """
        if not service_keys:
            return {}
        allocations = await self._asset_allocations(window_days, "LoadBalancer")
        if not allocations:
            return {}
        wanted = set(service_keys)
        costs: Dict[str, AssetCost] = {}
        for item in allocations:
            lbs = item.get("lbAllocations")
            if not isinstance(lbs, dict):
                continue
            for key, entry in lbs.items():
                if not isinstance(entry, dict):
                    continue
                service = entry.get("service")
                if not isinstance(service, str) or not service:
                    parts = str(key).split("/")
                    service = "/".join(parts[-2:]) if len(parts) >= 2 else str(key)
                cost = _optional_number(entry.get("cost"))
                if service not in wanted or cost is None:
                    continue
                previous = costs.get(service, AssetCost(0.0, 0.0))
                costs[service] = AssetCost(
                    cost=previous.cost + cost + _number(entry.get("adjustment")),
                    quantity_hours=max(previous.quantity_hours or 0.0, _number(entry.get("hours"))),
                )
        if costs:
            logger.info("Retrieved OpenCost LoadBalancer costs for %d Services.", len(costs))
        return costs

    async def collect_lb_costs(self, service_keys: Sequence[str], window_days: int = 7) -> Dict[str, float]:
        """Cost of specific LoadBalancer Services (``namespace/name``) over the last ``window_days``.

        See :meth:`collect_lb_cost_details`; an observed zero is kept as 0.0.
        """
        details = await self.collect_lb_cost_details(service_keys, window_days)
        return {key: detail.cost for key, detail in details.items()}

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
            r = await client.get(url, params=params, **self._auth_kwargs())
            r.raise_for_status()
            return True
        except Exception:
            return False
