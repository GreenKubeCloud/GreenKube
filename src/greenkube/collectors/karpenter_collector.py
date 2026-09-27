# src/greenkube/collectors/karpenter_collector.py
"""Collects Karpenter NodePool/NodeClaim state from the Kubernetes API.

Karpenter is optional: when the CRDs are not installed the collector degrades
gracefully and returns an empty list, like the VPA collector.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_custom_objects_api

logger = logging.getLogger(__name__)

KARPENTER_GROUP = "karpenter.sh"
KARPENTER_VERSIONS = ("v1", "v1beta1")
NODEPOOLS_PLURAL = "nodepools"
NODECLAIMS_PLURAL = "nodeclaims"


@dataclass
class NodePoolInfo:
    """A Karpenter NodePool with the nodes currently claimed from it."""

    name: str
    node_names: List[str] = field(default_factory=list)
    consolidation_policy: Optional[str] = None
    limits: dict = field(default_factory=dict)
    requirements: List[dict] = field(default_factory=list)
    status_resources: dict = field(default_factory=dict)

    @property
    def node_count(self) -> int:
        return len(self.node_names)


class KarpenterCollector:
    """Lists Karpenter NodePools and joins NodeClaims onto them."""

    def __init__(self):
        self._version: Optional[str] = None

    async def _resolve_version(self, api) -> Optional[str]:
        if self._version:
            return self._version
        for version in KARPENTER_VERSIONS:
            try:
                await api.list_cluster_custom_object(
                    group=KARPENTER_GROUP,
                    version=version,
                    plural=NODEPOOLS_PLURAL,
                    limit=1,
                    _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None,
                )
                self._version = version
                return version
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    continue
                logger.warning("Failed to list Karpenter NodePools (%s): %s", version, exc)
                return None
        return None

    async def collect(self) -> List[NodePoolInfo]:
        """Returns NodePools with their claimed node names, or an empty list."""
        api = await get_custom_objects_api()
        if not api:
            return []

        version = await self._resolve_version(api)
        if not version:
            logger.debug("Karpenter CRDs are not installed; skipping NodePool collection.")
            return []

        timeout = global_config.K8S_REQUEST_TIMEOUT or None
        try:
            pools_result = await api.list_cluster_custom_object(
                group=KARPENTER_GROUP,
                version=version,
                plural=NODEPOOLS_PLURAL,
                _request_timeout=timeout,
            )
            claims_result = await api.list_cluster_custom_object(
                group=KARPENTER_GROUP,
                version=version,
                plural=NODECLAIMS_PLURAL,
                _request_timeout=timeout,
            )
        except Exception as exc:
            logger.warning("Failed to collect Karpenter NodePools/NodeClaims: %s", exc)
            return []

        pools: dict[str, NodePoolInfo] = {}
        for item in (pools_result or {}).get("items", []) or []:
            metadata = item.get("metadata") or {}
            name = metadata.get("name")
            if not name:
                continue
            spec = item.get("spec") or {}
            disruption = spec.get("disruption") or {}
            status = item.get("status") or {}
            pools[name] = NodePoolInfo(
                name=name,
                consolidation_policy=disruption.get("consolidationPolicy"),
                limits=spec.get("limits") or {},
                requirements=spec.get("requirements") or [],
                status_resources=status.get("resources") or {},
            )

        for item in (claims_result or {}).get("items", []) or []:
            metadata = item.get("metadata") or {}
            labels = metadata.get("labels") or {}
            pool_name = labels.get("karpenter.sh/nodepool")
            if not pool_name or pool_name not in pools:
                continue
            status = item.get("status") or {}
            node_name = status.get("nodeName") or metadata.get("name")
            if node_name:
                pools[pool_name].node_names.append(node_name)

        result = list(pools.values())
        logger.info("Collected %d Karpenter NodePool(s).", len(result))
        return result
