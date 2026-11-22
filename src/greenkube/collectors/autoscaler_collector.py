# src/greenkube/collectors/autoscaler_collector.py
"""Reads the inputs of the realization factor from the Kubernetes API (roadmap B6).

* the Cluster Autoscaler status ConfigMap ``kube-system/cluster-autoscaler-status``
  (its presence means the autoscaler runs; its text or YAML is parsed defensively);
* Karpenter NodePools (consolidation policy and disruption budgets);
* nodes (Karpenter pool labels, ``do-not-disrupt`` / ``scale-down-disabled`` annotations);
* running pods (``do-not-disrupt`` / ``safe-to-evict`` annotations, labels for PDB selectors);
* PodDisruptionBudgets (``status.disruptionsAllowed``);
* when Karpenter runs: NodeClaims (claim → pool) and the ``DisruptionBlocked`` /
  ``Unconsolidatable`` events of NodePools, NodeClaims and Nodes (roadmap G5);
* when Karpenter runs and ``PROMETHEUS_URL`` is set: Karpenter's metrics
  ``karpenter_voluntary_disruption_decisions_total``,
  ``karpenter_voluntary_disruption_eligible_nodes`` and
  ``karpenter_nodepools_cost_total`` (roadmap G5; see :func:`karpenter_metric_queries`).

Every read is read-only and already allowed by the chart's ClusterRole. A read
that fails (missing CRD, RBAC, timeout) is reported in
``RealizationInputs.unavailable`` instead of failing the whole context. When
the Kubernetes API is not reachable at all, :meth:`collect` returns None and
the previous realization context stays in use.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_core_v1_api, get_custom_objects_api, get_policy_v1_api
from greenkube.impact.realization import (
    BLOCKING_KARPENTER_EVENTS,
    KARPENTER_METRICS_WINDOW,
    NOTED_KARPENTER_EVENTS,
    UNAVAILABLE_CLUSTER_AUTOSCALER,
    UNAVAILABLE_KARPENTER_METRICS,
    UNAVAILABLE_KARPENTER_POOLS,
    ClusterAutoscalerObservation,
    KarpenterEventObservation,
    KarpenterMetricsObservation,
    KarpenterPoolObservation,
    NodeObservation,
    PdbObservation,
    PodObservation,
    RealizationInputs,
)

logger = logging.getLogger(__name__)

CA_STATUS_NAMESPACE = "kube-system"
CA_STATUS_CONFIGMAP = "cluster-autoscaler-status"
KARPENTER_GROUP = "karpenter.sh"
KARPENTER_VERSIONS = ("v1", "v1beta1")
UNAVAILABLE_KARPENTER_EVENTS = "Karpenter events"

#: Instant PromQL query → its ``data.result`` list; None when the query failed.
MetricsQuery = Callable[[str], Awaitable[Optional[List[Dict[str, Any]]]]]
_QUERY_PATHS = ("/api/v1/query", "/prometheus/api/v1/query")


def karpenter_metric_queries(window: str = KARPENTER_METRICS_WINDOW) -> Dict[str, str]:
    """PromQL of the Karpenter metrics read into the RealizationContext, by observation field."""
    return {
        "decisions": f"sum by (reason) (increase(karpenter_voluntary_disruption_decisions_total[{window}]))",
        "eligible_nodes": "sum by (reason) (karpenter_voluntary_disruption_eligible_nodes)",
        "pool_costs": f"sum by (nodepool) (increase(karpenter_nodepools_cost_total[{window}]))",
    }


def _sample_value(sample: Any) -> Optional[float]:
    value = sample.get("value") if isinstance(sample, dict) else None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        number = float(value[1])
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def metric_by_label(result: Optional[List[Dict[str, Any]]], label: str) -> Optional[Dict[str, float]]:
    """``{label value: value}`` of an instant-query result; None when no series is exported."""
    if not result:
        return None
    values: Dict[str, float] = {}
    for sample in result:
        metric = sample.get("metric") if isinstance(sample, dict) else None
        key = (
            str((metric or {}).get(label) or "").lower() if label == "reason" else str((metric or {}).get(label) or "")
        )
        number = _sample_value(sample)
        if key and number is not None:
            values[key] = values.get(key, 0.0) + number
    return values


async def _prometheus_query(promql: str) -> Optional[List[Dict[str, Any]]]:
    """Instant query against ``PROMETHEUS_URL`` (same paths and credentials as the Prometheus collector)."""
    import httpx

    from greenkube.utils.http_client import get_async_http_client

    base = (global_config.PROMETHEUS_URL or "").rstrip("/")
    headers: Dict[str, str] = {}
    if global_config.PROMETHEUS_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {global_config.PROMETHEUS_BEARER_TOKEN}"
    username, password = global_config.PROMETHEUS_USERNAME, global_config.PROMETHEUS_PASSWORD
    auth = (username, password) if username and password else None
    async with get_async_http_client(verify=bool(global_config.PROMETHEUS_VERIFY_CERTS)) as client:
        for path in _QUERY_PATHS:
            try:
                response = await client.get(
                    f"{base}{path}",
                    params={"query": promql, "time": f"{datetime.now(timezone.utc).timestamp():.3f}"},
                    headers=headers,
                    auth=auth,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("status") == "success":
                result = (payload.get("data") or {}).get("result")
                return result if isinstance(result, list) else []
    return None


def _timeout() -> Optional[float]:
    return global_config.K8S_REQUEST_TIMEOUT or None


def _get(obj: Any, *names: str) -> Any:
    """Reads a field from a kubernetes_asyncio model or a plain dict."""
    for name in names:
        if obj is None:
            return None
        obj = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return obj


def karpenter_pool_from_item(item: dict) -> Optional[KarpenterPoolObservation]:
    """Parses one NodePool custom object (v1 or v1beta1)."""
    name = (item.get("metadata") or {}).get("name")
    if not name:
        return None
    disruption = (item.get("spec") or {}).get("disruption") or {}
    budgets = tuple(budget for budget in disruption.get("budgets") or [] if isinstance(budget, dict))
    return KarpenterPoolObservation(
        name=str(name),
        consolidation_policy=disruption.get("consolidationPolicy"),
        budgets=budgets,
    )


def pdb_from_object(pdb: Any) -> Optional[PdbObservation]:
    namespace = _get(pdb, "metadata", "namespace")
    name = _get(pdb, "metadata", "name")
    if not namespace or not name:
        return None
    selector = _get(pdb, "spec", "selector")
    match_labels = dict(_get(selector, "match_labels") or _get(selector, "matchLabels") or {})
    expressions: List[Tuple[str, str, Tuple[str, ...]]] = []
    for expression in _get(selector, "match_expressions") or _get(selector, "matchExpressions") or []:
        key = _get(expression, "key")
        operator = _get(expression, "operator")
        if key and operator:
            expressions.append((str(key), str(operator), tuple(str(v) for v in _get(expression, "values") or [])))
    allowed = _get(pdb, "status", "disruptions_allowed")
    if allowed is None:
        allowed = _get(pdb, "status", "disruptionsAllowed")
    expected = _get(pdb, "status", "expected_pods")
    if expected is None:
        expected = _get(pdb, "status", "expectedPods")
    return PdbObservation(
        namespace=str(namespace),
        name=str(name),
        match_labels=match_labels,
        match_expressions=tuple(expressions),
        disruptions_allowed=int(allowed) if allowed is not None else None,
        expected_pods=int(expected) if expected is not None else None,
    )


class AutoscalerCollector:
    """Collects :class:`RealizationInputs` from the cluster."""

    def __init__(self, metrics_query: Optional[MetricsQuery] = None) -> None:
        #: Prometheus instant query for the Karpenter metrics; the configured Prometheus by default.
        self._metrics_query = metrics_query

    async def collect(self) -> Optional[RealizationInputs]:
        core = await get_core_v1_api()
        if core is None:
            logger.debug("Kubernetes API unavailable; realization context not refreshed.")
            return None
        unavailable: List[str] = []

        try:
            node_list = await core.list_node(_request_timeout=_timeout())
        except Exception as error:
            logger.warning("Could not list nodes for the realization context: %s", error)
            return None
        nodes = [
            NodeObservation(
                name=str(_get(node, "metadata", "name")),
                labels=dict(_get(node, "metadata", "labels") or {}),
                annotations=dict(_get(node, "metadata", "annotations") or {}),
            )
            for node in getattr(node_list, "items", None) or []
            if _get(node, "metadata", "name")
        ]

        cluster_autoscaler = await self._cluster_autoscaler(core, unavailable)
        pods = await self._pods(core, unavailable)
        pdbs = await self._pdbs(unavailable)
        karpenter_pools = await self._karpenter_pools(unavailable)
        karpenter_events = await self._karpenter_events(core, unavailable) if karpenter_pools else []
        karpenter_metrics = await self._karpenter_metrics(unavailable) if karpenter_pools else None
        return RealizationInputs(
            nodes=nodes,
            pods=pods,
            pdbs=pdbs,
            karpenter_pools=karpenter_pools,
            cluster_autoscaler=cluster_autoscaler,
            karpenter_events=karpenter_events,
            karpenter_metrics=karpenter_metrics,
            unavailable=tuple(unavailable),
        )

    async def _karpenter_metrics(self, unavailable: List[str]) -> Optional[KarpenterMetricsObservation]:
        """Karpenter's Prometheus metrics; None without Prometheus, noted when Prometheus fails."""
        query = self._metrics_query
        if query is None:
            if not global_config.PROMETHEUS_URL:
                return None
            query = _prometheus_query
        results: Dict[str, Optional[Dict[str, float]]] = {}
        labels: Mapping[str, str] = {"decisions": "reason", "eligible_nodes": "reason", "pool_costs": "nodepool"}
        for name, promql in karpenter_metric_queries().items():
            try:
                result = await query(promql)
            except Exception as error:
                logger.debug("Karpenter metric query failed: %s", error)
                result = None
            if result is None:
                logger.warning("Could not read the Karpenter metrics from Prometheus; the factor ignores them.")
                unavailable.append(UNAVAILABLE_KARPENTER_METRICS)
                return None
            results[name] = metric_by_label(result, labels[name])
        return KarpenterMetricsObservation(
            decisions=results["decisions"],
            eligible_nodes=results["eligible_nodes"],
            pool_costs=results["pool_costs"],
        )

    async def _cluster_autoscaler(self, core: Any, unavailable: List[str]) -> Optional[ClusterAutoscalerObservation]:
        try:
            config_map = await core.read_namespaced_config_map(
                CA_STATUS_CONFIGMAP, CA_STATUS_NAMESPACE, _request_timeout=_timeout()
            )
        except Exception as error:
            if getattr(error, "status", None) != 404:
                logger.warning("Could not read %s/%s: %s", CA_STATUS_NAMESPACE, CA_STATUS_CONFIGMAP, error)
                unavailable.append(UNAVAILABLE_CLUSTER_AUTOSCALER)
            return None
        data = _get(config_map, "data") or {}
        status = data.get("status") if isinstance(data, dict) else None
        return ClusterAutoscalerObservation(status_text=str(status or ""))

    async def _pods(self, core: Any, unavailable: List[str]) -> Optional[List[PodObservation]]:
        try:
            pod_list = await core.list_pod_for_all_namespaces(
                field_selector="status.phase=Running", _request_timeout=_timeout()
            )
        except Exception as error:
            logger.warning("Could not list pods for the realization context: %s", error)
            unavailable.append("pods")
            return None
        return [
            PodObservation(
                namespace=str(_get(pod, "metadata", "namespace") or ""),
                name=str(_get(pod, "metadata", "name") or ""),
                node=_get(pod, "spec", "node_name") or _get(pod, "spec", "nodeName"),
                labels=dict(_get(pod, "metadata", "labels") or {}),
                annotations=dict(_get(pod, "metadata", "annotations") or {}),
            )
            for pod in getattr(pod_list, "items", None) or []
        ]

    async def _pdbs(self, unavailable: List[str]) -> Optional[List[PdbObservation]]:
        policy = await get_policy_v1_api()
        if policy is None:
            unavailable.append("PodDisruptionBudgets")
            return None
        try:
            pdb_list = await policy.list_pod_disruption_budget_for_all_namespaces(_request_timeout=_timeout())
        except Exception as error:
            logger.warning("Could not list PodDisruptionBudgets for the realization context: %s", error)
            unavailable.append("PodDisruptionBudgets")
            return None
        return [pdb for pdb in (pdb_from_object(item) for item in getattr(pdb_list, "items", None) or []) if pdb]

    async def _karpenter_pools(self, unavailable: List[str]) -> Optional[List[KarpenterPoolObservation]]:
        api = await get_custom_objects_api()
        if api is None:
            return []
        for version in KARPENTER_VERSIONS:
            try:
                result = await api.list_cluster_custom_object(
                    group=KARPENTER_GROUP, version=version, plural="nodepools", _request_timeout=_timeout()
                )
            except Exception as error:
                if getattr(error, "status", None) == 404:
                    continue
                logger.warning("Could not list Karpenter NodePools (%s): %s", version, error)
                unavailable.append(UNAVAILABLE_KARPENTER_POOLS)
                return None
            items = (result or {}).get("items", []) if isinstance(result, dict) else []
            return [pool for pool in (karpenter_pool_from_item(item) for item in items) if pool]
        return []

    async def _karpenter_claim_pools(self) -> Dict[str, str]:
        """NodeClaim name → NodePool (``karpenter.sh/nodepool`` label); empty when unreadable."""
        api = await get_custom_objects_api()
        if api is None:
            return {}
        for version in KARPENTER_VERSIONS:
            try:
                result = await api.list_cluster_custom_object(
                    group=KARPENTER_GROUP, version=version, plural="nodeclaims", _request_timeout=_timeout()
                )
            except Exception as error:
                if getattr(error, "status", None) == 404:
                    continue
                logger.debug("Could not list Karpenter NodeClaims (%s): %s", version, error)
                return {}
            items = (result or {}).get("items", []) if isinstance(result, dict) else []
            return karpenter_claim_pools(items)
        return {}

    async def _karpenter_events(self, core: Any, unavailable: List[str]) -> Optional[List[KarpenterEventObservation]]:
        claim_pools = await self._karpenter_claim_pools()
        events: List[KarpenterEventObservation] = []
        for reason in sorted(BLOCKING_KARPENTER_EVENTS | NOTED_KARPENTER_EVENTS):
            try:
                event_list = await core.list_event_for_all_namespaces(
                    field_selector=f"reason={reason}", _request_timeout=_timeout()
                )
            except Exception as error:
                logger.warning("Could not list Karpenter %s events: %s", reason, error)
                unavailable.append(UNAVAILABLE_KARPENTER_EVENTS)
                return None
            for item in getattr(event_list, "items", None) or []:
                event = karpenter_event_from_object(item, claim_pools)
                if event is not None:
                    events.append(event)
        return events


def karpenter_claim_pools(items: List[Any]) -> Dict[str, str]:
    pools: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        pool = (metadata.get("labels") or {}).get("karpenter.sh/nodepool")
        if name and pool:
            pools[str(name)] = str(pool)
    return pools


def karpenter_event_from_object(event: Any, claim_pools: Dict[str, str]) -> Optional[KarpenterEventObservation]:
    """A Karpenter event about a NodePool, NodeClaim or Node; None for other objects."""
    kind = _get(event, "involved_object", "kind") or _get(event, "involvedObject", "kind")
    name = _get(event, "involved_object", "name") or _get(event, "involvedObject", "name")
    reason = _get(event, "reason")
    if not kind or not name or not reason or kind not in ("NodePool", "NodeClaim", "Node"):
        return None
    pool = str(name) if kind == "NodePool" else claim_pools.get(str(name)) if kind == "NodeClaim" else None
    return KarpenterEventObservation(
        reason=str(reason),
        kind=str(kind),
        name=str(name),
        pool=pool,
        message=str(_get(event, "message") or "")[:300],
    )
