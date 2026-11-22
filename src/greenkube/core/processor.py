# src/greenkube/core/processor.py
"""DataProcessor – composition facade for range collection.

Wires the collectors and collaborators used by the production collection
path and delegates the work to them:

* :class:`HistoricalRangeProcessor` – chunked range processing ``[start, end)``
* :class:`MetricAssembler` – estimation flags and container rows
* :class:`NodeZoneMapper` – cloud zone → Electricity Maps zone
* :class:`EmbodiedEmissionsService` – Boavizta embodied emissions
"""

import logging
from typing import List

from ..collectors.base_electricity_provider import BaseElectricityProvider
from ..collectors.boavizta_collector import BoaviztaCollector
from ..collectors.metrics_server_collector import MetricsServerCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..core.calculator import CarbonCalculator
from ..core.config import Config, get_config
from ..core.embodied_service import EmbodiedEmissionsService
from ..core.historical_range_processor import HistoricalRangeProcessor
from ..core.metric_assembler import MetricAssembler
from ..core.node_zone_mapper import NodeZoneMapper
from ..energy.estimator import BasicEstimator
from ..models.metrics import CombinedMetric
from ..storage.base_pod_snapshot_repository import PodSnapshotRepository
from ..storage.base_repository import CarbonIntensityRepository, CombinedMetricsRepository, NodeRepository
from ..storage.embodied_repository import EmbodiedRepository

logger = logging.getLogger(__name__)


class DataProcessor:
    """Builds the range-collection pipeline and owns its collectors' lifecycle."""

    def __init__(
        self,
        prometheus_collector: PrometheusCollector,
        opencost_collector: OpenCostCollector,
        node_collector: NodeCollector,
        pod_collector: PodCollector,
        electricity_provider: BaseElectricityProvider,
        boavizta_collector: BoaviztaCollector,
        repository: CarbonIntensityRepository,
        combined_metrics_repository: CombinedMetricsRepository,
        node_repository: NodeRepository,
        embodied_repository: EmbodiedRepository,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
        config: Config | None = None,
        metrics_server_collector: MetricsServerCollector | None = None,
        pod_snapshot_repository: PodSnapshotRepository | None = None,
    ):
        self._config = config if config is not None else get_config()
        self.calculator = calculator
        self.estimator = estimator
        self.prometheus_collector = prometheus_collector
        self.opencost_collector = opencost_collector
        self.node_collector = node_collector
        self.pod_collector = pod_collector
        self.electricity_provider = electricity_provider
        self.boavizta_collector = boavizta_collector
        self.repository = repository
        self.embodied_repository = embodied_repository
        self._metrics_server_collector = metrics_server_collector or MetricsServerCollector()

        self._zone_mapper = NodeZoneMapper(node_collector=node_collector, config=self._config)
        self._embodied_service = EmbodiedEmissionsService(
            boavizta_collector=boavizta_collector,
            embodied_repository=embodied_repository,
            node_repository=node_repository,
            calculator=calculator,
            estimator=estimator,
            config=self._config,
        )
        self._assembler = MetricAssembler(
            calculator=calculator,
            estimator=estimator,
            repository=repository,
            electricity_provider=electricity_provider,
            zone_mapper=self._zone_mapper,
            embodied_service=self._embodied_service,
            config=self._config,
        )
        self._range_processor = HistoricalRangeProcessor(
            prometheus_collector=prometheus_collector,
            opencost_collector=opencost_collector,
            node_collector=node_collector,
            pod_collector=pod_collector,
            repository=repository,
            combined_metrics_repository=combined_metrics_repository,
            node_repository=node_repository,
            calculator=calculator,
            estimator=estimator,
            assembler=self._assembler,
            zone_mapper=self._zone_mapper,
            config=self._config,
            metrics_server_collector=self._metrics_server_collector,
            pod_snapshot_repository=pod_snapshot_repository,
        )

    async def run_range(
        self,
        start,
        end,
        step=None,
        namespace=None,
    ) -> List[CombinedMetric]:
        """Collect the half-open window ``[start, end)`` (see :class:`HistoricalRangeProcessor`)."""
        return await self._range_processor.run_range(start=start, end=end, step=step, namespace=namespace)

    @property
    def last_idle_metrics(self) -> list:
        """``__idle__`` rows of the last :meth:`run_range` call (committed by the caller)."""
        return self._range_processor.last_idle_metrics

    async def close(self):
        """Close all collectors to release resources."""
        await self.prometheus_collector.close()
        await self.opencost_collector.close()
        await self.pod_collector.close()
        await self.node_collector.close()
        await self.electricity_provider.close()
        await self.boavizta_collector.close()
        logger.debug("DataProcessor closed all collectors.")
