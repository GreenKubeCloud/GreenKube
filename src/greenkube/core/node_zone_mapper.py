# src/greenkube/core/node_zone_mapper.py
"""Maps Kubernetes node cloud zones to Electricity Maps zones."""

import logging
from typing import Dict, List, Optional

from ..collectors.node_collector import NodeCollector
from ..core.config import Config, get_config
from ..models.node import NodeInfo, NodeZoneContext
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone

logger = logging.getLogger(__name__)


class NodeZoneMapper:
    """Resolves cloud provider zones to Electricity Maps zones for each node."""

    def __init__(self, node_collector: NodeCollector, config: Config | None = None):
        self.node_collector = node_collector
        self._config = config if config is not None else get_config()

    async def map_nodes(self, nodes_info: Optional[Dict[str, NodeInfo]] = None) -> Dict[str, NodeZoneContext]:
        """Collect node zones and map them to Electricity Maps zones.

        Args:
            nodes_info: Optional dict[str, NodeInfo] from node collector.

        Returns:
            A dict mapping node names to NodeZoneContext objects.
        """
        if nodes_info is None:
            try:
                nodes_info = await self.node_collector.collect()
                if not nodes_info:
                    logger.warning(
                        "NodeCollector returned no zones. Using default zone '%s'.",
                        self._config.DEFAULT_ZONE,
                    )
            except Exception as e:
                logger.error(
                    "Failed to collect node zones: %s. Using default zone '%s'.",
                    e,
                    self._config.DEFAULT_ZONE,
                )
                nodes_info = {}

        node_contexts: Dict[str, NodeZoneContext] = {}
        if not nodes_info:
            return node_contexts

        for node_name, node_info in nodes_info.items():
            cloud_zone = node_info.zone
            provider = node_info.cloud_provider
            mapped = None
            reasons: List[str] = []
            is_estimated = False

            if cloud_zone:
                try:
                    mapped = get_emaps_zone_from_cloud_zone(cloud_zone, provider=provider)
                except Exception:
                    logger.warning(
                        "Exception while mapping cloud zone '%s' for node '%s'.",
                        cloud_zone,
                        node_name,
                        exc_info=True,
                    )

            if mapped:
                logger.info(
                    "Node '%s' cloud zone '%s' (provider: %s) -> Electricity Maps zone '%s'",
                    node_name,
                    cloud_zone,
                    provider,
                    mapped,
                )
            else:
                region = node_info.region
                if region:
                    try:
                        mapped = get_emaps_zone_from_cloud_zone(region, provider=provider)
                    except Exception:
                        logger.warning(
                            "Failed to map region '%s' (provider: %s).",
                            region,
                            provider,
                            exc_info=True,
                        )

                if mapped:
                    reasons.append(
                        f"Node '{node_name}' region '{region}' (provider: {provider}) -> "
                        f"Electricity Maps zone '{mapped}' (fallback from zone '{cloud_zone}')"
                    )
                    is_estimated = True
                    logger.info(
                        "Node '%s' region '%s' (provider: %s) -> Electricity Maps zone '%s' (fallback from zone '%s')",
                        node_name,
                        region,
                        provider,
                        mapped,
                        cloud_zone,
                    )
                else:
                    mapped = self._config.DEFAULT_ZONE
                    reasons.append(
                        f"Could not map cloud zone '{cloud_zone}' or region '{region}'. "
                        f"Used default zone '{self._config.DEFAULT_ZONE}'"
                    )
                    is_estimated = True
                    logger.warning(
                        "Could not map cloud zone '%s' or region '%s' for node '%s'. Using default: '%s'",
                        cloud_zone,
                        region,
                        node_name,
                        self._config.DEFAULT_ZONE,
                    )

            node_contexts[node_name] = NodeZoneContext(
                node=node_name,
                emaps_zone=mapped,
                is_estimated=is_estimated,
                estimation_reasons=reasons,
            )

        return node_contexts
