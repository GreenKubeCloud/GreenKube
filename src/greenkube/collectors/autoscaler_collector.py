# src/greenkube/collectors/autoscaler_collector.py
"""Reads the inputs of the realization factor from the Kubernetes API (roadmap B6).

* the Cluster Autoscaler status ConfigMap ``kube-system/cluster-autoscaler-status``
  (its presence means the autoscaler runs; its text or YAML is parsed defensively);
* Karpenter NodePools (consolidation policy and disruption budgets);
* nodes (Karpenter pool labels, ``do-not-disrupt`` / ``scale-down-disabled`` annotations);
* running pods (``do-not-disrupt`` / ``safe-to-evict`` annotations, labels for PDB selectors);
* PodDisruptionBudgets (``status.disruptionsAllowed``).

Every read is read-only and already allowed by the chart's ClusterRole. A read
that fails (missing CRD, RBAC, timeout) is reported in
``RealizationInputs.unavailable`` instead of failing the whole context. When
the Kubernetes API is not reachable at all, :meth:`collect` returns None and
the previous realization context stays in use.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_core_v1_api, get_custom_objects_api, get_policy_v1_api
from greenkube.impact.realization import (
    ClusterAutoscalerObservation,
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
        return RealizationInputs(
            nodes=nodes,
            pods=pods,
            pdbs=pdbs,
            karpenter_pools=karpenter_pools,
            cluster_autoscaler=cluster_autoscaler,
            unavailable=tuple(unavailable),
        )

    async def _cluster_autoscaler(self, core: Any, unavailable: List[str]) -> Optional[ClusterAutoscalerObservation]:
        try:
            config_map = await core.read_namespaced_config_map(
                CA_STATUS_CONFIGMAP, CA_STATUS_NAMESPACE, _request_timeout=_timeout()
            )
        except Exception as error:
            if getattr(error, "status", None) != 404:
                logger.warning("Could not read %s/%s: %s", CA_STATUS_NAMESPACE, CA_STATUS_CONFIGMAP, error)
                unavailable.append("Cluster Autoscaler status")
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
                unavailable.append("Karpenter NodePools")
                return None
            items = (result or {}).get("items", []) if isinstance(result, dict) else []
            return [pool for pool in (karpenter_pool_from_item(item) for item in items) if pool]
        return []
