# src/greenkube/core/processor.py
"""DataProcessor – thin orchestration facade.

Delegates the heavy lifting to focused collaborators:

* :class:`CollectionOrchestrator` – parallel data collection
* :class:`MetricAssembler` – CombinedMetric construction
* :class:`NodeZoneMapper` – cloud zone → Electricity Maps zone
* :class:`EmbodiedEmissionsService` – Boavizta embodied emissions
* :class:`PrometheusResourceMapper` – per-pod resource maps from Prometheus
* :class:`CostNormalizer` – per-step / per-range cost normalisation
* :class:`HistoricalRangeProcessor` – chunked historical range processing
"""

import logging
from typing import Dict, List, Set

from ..collectors.boavizta_collector import BoaviztaCollector
from ..collectors.electricity_maps_collector import ElectricityMapsCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..core.calculator import CarbonCalculator
from ..core.collection_orchestrator import CollectionOrchestrator
from ..core.config import config
from ..core.embodied_service import EmbodiedEmissionsService
from ..core.historical_range_processor import HistoricalRangeProcessor
from ..core.metric_assembler import MetricAssembler
from ..core.node_zone_mapper import NodeZoneMapper
from ..core.prometheus_resource_mapper import PrometheusResourceMapper
from ..energy.estimator import BasicEstimator
from ..models.metrics import CombinedMetric
from ..storage.base_repository import CarbonIntensityRepository, NodeRepository
from ..storage.embodied_repository import EmbodiedRepository

logger = logging.getLogger(__name__)


class DataProcessor:
    """Orchestrates data collection, energy estimation, and carbon calculation.

    Each responsibility is delegated to a specialised collaborator.
    """

    def __init__(
        self,
        prometheus_collector: PrometheusCollector,
        opencost_collector: OpenCostCollector,
        node_collector: NodeCollector,
        pod_collector: PodCollector,
        electricity_maps_collector: ElectricityMapsCollector,
        boavizta_collector: BoaviztaCollector,
        repository: CarbonIntensityRepository,
        node_repository: NodeRepository,
        embodied_repository: EmbodiedRepository,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
    ):
        self.calculator = calculator
        self.estimator = estimator
        self.node_collector = node_collector
        self.electricity_maps_collector = electricity_maps_collector
        self.boavizta_collector = boavizta_collector
        self.repository = repository
        self.embodied_repository = embodied_repository

        # --- Internal collaborators ---
        self._orchestrator = CollectionOrchestrator(
            prometheus_collector=prometheus_collector,
            opencost_collector=opencost_collector,
            node_collector=node_collector,
            pod_collector=pod_collector,
        )
        self._zone_mapper = NodeZoneMapper(node_collector=node_collector)
        self._embodied_service = EmbodiedEmissionsService(
            boavizta_collector=boavizta_collector,
            embodied_repository=embodied_repository,
            node_repository=node_repository,
            calculator=calculator,
            estimator=estimator,
        )
        self._assembler = MetricAssembler(
            calculator=calculator,
            estimator=estimator,
            repository=repository,
            electricity_maps_collector=electricity_maps_collector,
            zone_mapper=self._zone_mapper,
            embodied_service=self._embodied_service,
        )
        self._range_processor = HistoricalRangeProcessor(
            prometheus_collector=prometheus_collector,
            opencost_collector=opencost_collector,
            node_collector=node_collector,
            pod_collector=pod_collector,
            repository=repository,
            node_repository=node_repository,
            calculator=calculator,
            estimator=estimator,
            assembler=self._assembler,
            zone_mapper=self._zone_mapper,
        )

    # ------------------------------------------------------------------
    # run()
    # ------------------------------------------------------------------

    async def run(self) -> List[CombinedMetric]:
        """Execute the data processing pipeline."""
        logger.info("Starting data processing cycle...")

        # 1. Collect from all sources in parallel
        cr = await self._orchestrator.collect_all()

        prom_metrics = cr.prom_metrics
        node_instance_map = cr.node_instance_map
        cost_map = cr.cost_map
        pod_request_map_simple = cr.pod_request_map_simple
        pod_request_map = cr.pod_request_map_agg
        nodes_info = cr.nodes_info

        # 2. Adjust low-CPU nodes with pod requests
        cpu_adjusted_nodes: Set[str] = set()
        if prom_metrics:
            try:
                node_totals: Dict[str, float] = {}
                for item in prom_metrics.pod_cpu_usage:
                    node_totals.setdefault(item.node, 0.0)
                    node_totals[item.node] += item.cpu_usage_cores

                LOW_NODE_CPU_THRESHOLD = config.LOW_NODE_CPU_THRESHOLD
                if node_totals:
                    node_to_items: Dict[str, list] = {}
                    for item in prom_metrics.pod_cpu_usage:
                        node_to_items.setdefault(item.node, []).append(item)

                    for node, total_cpu in node_totals.items():
                        if total_cpu < LOW_NODE_CPU_THRESHOLD:
                            total_reqs = 0.0
                            for itm in node_to_items.get(node, []):
                                total_reqs += pod_request_map_simple.get((itm.namespace, itm.pod), 0.0)

                            if total_reqs > 0:
                                cpu_adjusted_nodes.add(node)
                                logger.info(
                                    "Node '%s' CPU %.4f below threshold; substituting pod requests (%.4f)",
                                    node,
                                    total_cpu,
                                    total_reqs,
                                )
                                for itm in node_to_items.get(node, []):
                                    req = pod_request_map_simple.get((itm.namespace, itm.pod), 0.0)
                                    if req:
                                        itm.cpu_usage_cores = req
            except Exception as e:
                logger.warning(
                    "Failed to adjust node utilization based on pod requests: %s",
                    e,
                    exc_info=True,
                )

            # 3. Estimate energy
            try:
                energy_metrics = self.estimator.estimate(prom_metrics)
                logger.info(
                    "Successfully estimated %d energy metrics from Prometheus.",
                    len(energy_metrics),
                )
            except Exception as e:
                logger.error("Estimator failed: %s", e)
                energy_metrics = []
        else:
            energy_metrics = []

        # 4. Build per-pod resource maps from Prometheus data
        resource_maps = PrometheusResourceMapper.build(prom_metrics)

        # 5. Node zone mapping
        node_contexts = await self._zone_mapper.map_nodes(nodes_info)

        # 6. Prefetch carbon intensities
        await self._assembler.prefetch_intensities(energy_metrics, node_contexts)

        # 7. Ensure node instance map
        if not node_instance_map:
            try:
                node_instance_map = await self.node_collector.collect_instance_types() or {}
            except Exception:
                node_instance_map = {}

        # 8. Prepare embodied emissions (Boavizta)
        boavizta_cache = await self._embodied_service.prepare_embodied_data(nodes_info)

        # 9. Assemble CombinedMetrics
        steps_per_day = 86400 / self.estimator.query_range_step_sec

        combined_metrics = await self._assembler.assemble(
            energy_metrics=energy_metrics,
            cost_map=cost_map,
            pod_request_map=pod_request_map,
            node_contexts=node_contexts,
            nodes_info=nodes_info,
            node_instance_map=node_instance_map,
            boavizta_cache=boavizta_cache,
            cpu_adjusted_nodes=cpu_adjusted_nodes,
            steps_per_day=steps_per_day,
            resource_maps=resource_maps,
        )

        logger.info(
            "Processing complete. Found %d combined metrics.",
            len(combined_metrics),
        )
        await self.calculator.clear_cache()
        return combined_metrics

    # ------------------------------------------------------------------
    # run_range()
    # ------------------------------------------------------------------

    async def run_range(
        self,
        start,
        end,
        step=None,
        namespace=None,
    ) -> List[CombinedMetric]:
        """Delegate historical range processing."""
        return await self._range_processor.run_range(start=start, end=end, step=step, namespace=namespace)

    # ------------------------------------------------------------------
    # close()
    # ------------------------------------------------------------------

    async def close(self):
        """Close all collectors to release resources."""
        await self._orchestrator.close()
        await self.electricity_maps_collector.close()
        await self.boavizta_collector.close()
        logger.debug("DataProcessor closed all collectors.")
