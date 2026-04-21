# src/greenkube/core/collection_orchestrator.py
"""Orchestrates parallel data collection from Prometheus, OpenCost, and Pods.

Node collection is intentionally excluded from this orchestrator.  Nodes must
be collected first (Phase 1 in DataProcessor.run), so that zone mapping,
Boavizta, and Prometheus instance-type enrichment all operate on already-
resolved node data.  Passing ``nodes_info`` here avoids any duplicate K8s API
calls and race conditions on the shared Kubernetes client.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..models.metrics import CostMetric, PodMetric
from ..models.node import NodeInfo
from ..models.prometheus_metrics import NodeInstanceType, PrometheusMetric

logger = logging.getLogger(__name__)


@dataclass
class CollectionResult:
    """Holds all data collected from external sources in a single cycle."""

    prom_metrics: Optional[PrometheusMetric] = None
    node_instance_map: Dict[str, str] = field(default_factory=dict)
    cost_map: Dict[str, CostMetric] = field(default_factory=dict)
    pod_metrics_list: List[PodMetric] = field(default_factory=list)
    pod_request_map_simple: Dict[tuple, float] = field(default_factory=dict)
    pod_request_map_agg: dict = field(default_factory=dict)


class CollectionOrchestrator:
    """Fetches data in parallel from Prometheus, OpenCost, and Pods.

    Node information (``nodes_info``) must be supplied by the caller — it is
    collected in Phase 1 of :meth:`DataProcessor.run` before this orchestrator
    is invoked, ensuring zone resolution and instance-type enrichment always
    use up-to-date K8s data without triggering redundant API calls.
    """

    def __init__(
        self,
        prometheus_collector: PrometheusCollector,
        opencost_collector: OpenCostCollector,
        pod_collector: PodCollector,
    ):
        self.prometheus_collector = prometheus_collector
        self.opencost_collector = opencost_collector
        self.pod_collector = pod_collector

    async def collect_all(self, nodes_info: Optional[Dict[str, NodeInfo]] = None) -> CollectionResult:
        """Execute Prometheus, OpenCost, and Pod collectors in parallel.

        Args:
            nodes_info: Node data already collected in Phase 1.  Used to
                enrich the PrometheusMetric with instance-type information
                when Prometheus labels are absent, without making a new K8s
                API call.
        """
        nodes_info = nodes_info or {}

        async def fetch_prometheus():
            try:
                prom_metrics = await self.prometheus_collector.collect()
                node_instance_map: Dict[str, str] = {}

                node_types = getattr(prom_metrics, "node_instance_types", None)
                if not node_types:
                    # Enrich from already-collected nodes_info (no extra K8s call).
                    if nodes_info:
                        if getattr(prom_metrics, "node_instance_types", None) is None:
                            try:
                                prom_metrics.node_instance_types = []
                            except Exception:
                                pass

                        for node_name, info in nodes_info.items():
                            if info.instance_type:
                                prom_metrics.node_instance_types.append(
                                    NodeInstanceType(node=node_name, instance_type=info.instance_type)
                                )
                                node_instance_map[node_name] = info.instance_type

                        if node_instance_map:
                            logger.info(
                                "Enriched PrometheusMetric with %d instance-type(s) from Phase-1 node data.",
                                len(node_instance_map),
                            )
                else:
                    for item in node_types:
                        node_instance_map[item.node] = item.instance_type

                return prom_metrics, node_instance_map
            except Exception as e:
                logger.error("Failed to collect/estimate energy metrics from Prometheus: %s", e)
                return None, {}

        async def fetch_opencost():
            try:
                cost_metrics = await self.opencost_collector.collect()
                logger.info("Successfully collected %d metrics from OpenCost.", len(cost_metrics))
                return {metric.pod_name: metric for metric in cost_metrics if metric.pod_name}
            except Exception as e:
                logger.error("Failed to collect data from OpenCost: %s", e)
                return {}

        async def fetch_pods():
            try:
                pod_metrics = await self.pod_collector.collect()
                req_map = {(pm.namespace, pm.pod_name): pm.cpu_request / 1000.0 for pm in pod_metrics}

                agg_map = defaultdict(
                    lambda: {
                        "cpu": 0,
                        "memory": 0,
                        "ephemeral_storage": 0,
                        "owner_kind": None,
                        "owner_name": None,
                    }
                )
                for pm in pod_metrics:
                    key = (pm.namespace, pm.pod_name)
                    agg_map[key]["cpu"] += pm.cpu_request
                    agg_map[key]["memory"] += pm.memory_request
                    agg_map[key]["ephemeral_storage"] += pm.ephemeral_storage_request
                    if pm.owner_kind and not agg_map[key]["owner_kind"]:
                        agg_map[key]["owner_kind"] = pm.owner_kind
                        agg_map[key]["owner_name"] = pm.owner_name

                return pod_metrics, req_map, agg_map
            except Exception as e:
                logger.error("Failed to collect data from PodCollector: %s", e)
                return [], {}, {}

        prom_result, opencost_result, pod_result = await asyncio.gather(
            fetch_prometheus(), fetch_opencost(), fetch_pods()
        )

        prom_metrics, node_instance_map = prom_result
        cost_map = opencost_result
        pod_metrics_list, pod_request_map_simple, pod_request_map_agg = pod_result

        return CollectionResult(
            prom_metrics=prom_metrics,
            node_instance_map=node_instance_map,
            cost_map=cost_map,
            pod_metrics_list=pod_metrics_list,
            pod_request_map_simple=pod_request_map_simple,
            pod_request_map_agg=pod_request_map_agg,
        )

    async def close(self):
        """Close all collectors to release resources."""
        await self.prometheus_collector.close()
        await self.opencost_collector.close()
        await self.pod_collector.close()
        logger.debug("CollectionOrchestrator closed all collectors.")
