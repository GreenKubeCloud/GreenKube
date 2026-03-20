# src/greenkube/core/metric_assembler.py
"""Assembles CombinedMetric objects from collected data."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from .. import __version__
from ..collectors.electricity_maps_collector import ElectricityMapsCollector
from ..core.calculator import CarbonCalculator
from ..core.config import Config, get_config
from ..core.cost_normalizer import CostNormalizer
from ..core.embodied_service import EmbodiedEmissionsService
from ..core.node_zone_mapper import NodeZoneMapper
from ..core.prometheus_resource_mapper import PodResourceMaps
from ..energy.estimator import BasicEstimator
from ..models.metrics import CombinedMetric, CostMetric, EnergyMetric
from ..models.node import NodeInfo, NodeZoneContext
from ..storage.base_repository import CarbonIntensityRepository
from ..utils.date_utils import parse_iso_date

logger = logging.getLogger(__name__)


class MetricAssembler:
    """Builds CombinedMetric instances from energy, cost, node, and pod data.

    This class is intentionally thin. Zone mapping is handled by
    :class:`NodeZoneMapper`, embodied emissions by
    :class:`EmbodiedEmissionsService`, and resource maps by
    :class:`PrometheusResourceMapper`.
    """

    def __init__(
        self,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
        repository: CarbonIntensityRepository,
        electricity_maps_collector: ElectricityMapsCollector,
        zone_mapper: NodeZoneMapper,
        embodied_service: EmbodiedEmissionsService,
        config: Config | None = None,
    ):
        self.calculator = calculator
        self.estimator = estimator
        self.repository = repository
        self.electricity_maps_collector = electricity_maps_collector
        self.zone_mapper = zone_mapper
        self.embodied_service = embodied_service
        self._config = config if config is not None else get_config()

    # ------------------------------------------------------------------
    # Carbon-intensity prefetch
    # ------------------------------------------------------------------

    async def prefetch_intensities(
        self,
        energy_metrics: List[EnergyMetric],
        node_contexts: Dict[str, NodeZoneContext],
    ) -> None:
        """Group energy metrics by zone and prefetch carbon intensities."""
        zone_to_metrics: Dict[str, List[EnergyMetric]] = {}
        for em in energy_metrics:
            node_name = em.node
            context = node_contexts.get(node_name)
            emaps_zone = context.emaps_zone if context else self._config.DEFAULT_ZONE
            zone_to_metrics.setdefault(emaps_zone, []).append(em)

        async def _prefetch_zone(zone: str, metrics: List[EnergyMetric]) -> None:
            representative_ts = max(m.timestamp for m in metrics)
            gran = getattr(self._config, "NORMALIZATION_GRANULARITY", "hour")
            if isinstance(representative_ts, str):
                rep_dt = parse_iso_date(representative_ts)
                if not rep_dt:
                    rep_dt = datetime.now(timezone.utc)
            else:
                rep_dt = representative_ts

            if gran == "hour":
                rep_normalized_dt = rep_dt.replace(minute=0, second=0, microsecond=0)
            elif gran == "day":
                rep_normalized_dt = rep_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                rep_normalized_dt = rep_dt

            rep_dt_utc = rep_normalized_dt.astimezone(timezone.utc).replace(microsecond=0)
            rep_normalized_plus = rep_dt_utc.isoformat()
            try:
                intensity = await self.repository.get_for_zone_at_time(zone, rep_normalized_plus)
                if intensity is None:
                    logger.info(
                        "Intensity missing for zone %s at %s. Attempting live fetch.",
                        zone,
                        rep_normalized_plus,
                    )
                    history = await self.electricity_maps_collector.collect(
                        zone=zone, target_datetime=rep_normalized_dt
                    )
                    if history:
                        await self.repository.save_history(history, zone)
                    intensity = await self.repository.get_for_zone_at_time(zone, rep_normalized_plus)
                logger.info(
                    "Prefetched intensity for zone '%s' at '%s' (present=%s)",
                    zone,
                    rep_normalized_plus,
                    intensity is not None,
                )
            except Exception as e:
                intensity = None
                logger.warning(
                    "Failed to prefetch intensity for zone '%s' at '%s': %s",
                    zone,
                    rep_normalized_plus,
                    e,
                )

            if intensity is not None:
                for m in metrics:
                    ts = m.timestamp
                    if isinstance(ts, str):
                        dt = parse_iso_date(ts)
                        if not dt:
                            dt = rep_dt
                    else:
                        dt = ts
                    ts_str = ts if isinstance(ts, str) else dt.isoformat()
                    await self.calculator.prefetch_intensity(zone, ts_str, intensity)

        if zone_to_metrics:
            logger.info(
                "Prefetching intensity for %d zones in parallel...",
                len(zone_to_metrics),
            )
            await asyncio.gather(*(_prefetch_zone(z, m) for z, m in zone_to_metrics.items()))

    # ------------------------------------------------------------------
    # Estimation-flags helper
    # ------------------------------------------------------------------

    def build_estimation_flags(
        self,
        energy_metric: EnergyMetric,
        node_context: Optional[NodeZoneContext],
        cost_metric: Optional[object],
        provider: Optional[str],
        pue: float,
        node_name: str,
        cpu_adjusted_nodes: Set[str],
    ) -> tuple:
        """Return ``(is_estimated, estimation_reasons)``."""
        estimation_reasons: List[str] = []
        is_estimated = False

        if energy_metric.is_estimated:
            is_estimated = True
            estimation_reasons.extend(energy_metric.estimation_reasons)

        if node_context:
            if node_context.is_estimated:
                is_estimated = True
                estimation_reasons.extend(node_context.estimation_reasons)
        else:
            is_estimated = True
            estimation_reasons.append(
                f"Node '{node_name}' not found in zone map. Used default zone '{self._config.DEFAULT_ZONE}'"
            )

        if not cost_metric:
            is_estimated = True
            estimation_reasons.append(
                f"No cost data for pod '{energy_metric.pod_name}'. Used default cost {self._config.DEFAULT_COST}"
            )

        if not provider:
            is_estimated = True
            estimation_reasons.append(f"Unknown provider for node '{node_name}'. Used default PUE {pue}")
        elif f"default_{provider.lower()}" not in self._config.DATACENTER_PUE_PROFILES:
            is_estimated = True
            estimation_reasons.append(f"No PUE profile for provider '{provider}'. Used default PUE {pue}")

        if node_name in cpu_adjusted_nodes:
            is_estimated = True
            estimation_reasons.append(f"CPU usage on node '{node_name}' was below threshold; substituted pod requests")

        return is_estimated, estimation_reasons

    # ------------------------------------------------------------------
    # Assemble CombinedMetric list
    # ------------------------------------------------------------------

    async def assemble(
        self,
        energy_metrics: List[EnergyMetric],
        cost_map: Dict[str, CostMetric],
        pod_request_map: dict,
        node_contexts: Dict[str, NodeZoneContext],
        nodes_info: Dict[str, NodeInfo],
        node_instance_map: Dict[str, str],
        boavizta_cache: Dict[tuple, dict],
        cpu_adjusted_nodes: Set[str],
        steps_per_day: float,
        resource_maps: PodResourceMaps,
    ) -> List[CombinedMetric]:
        """Build CombinedMetric objects from all collected & processed data."""
        combined_metrics: List[CombinedMetric] = []

        for energy_metric in energy_metrics:
            pod_name = energy_metric.pod_name
            namespace = energy_metric.namespace
            pod_key = (namespace, pod_name)

            # Cost
            cost_metric = cost_map.get(pod_name)
            total_cost = CostNormalizer.per_step_cost(cost_map, pod_name, steps_per_day)

            # Pod requests
            pod_requests = pod_request_map.get(pod_key, {"cpu": 0, "memory": 0})

            # Zone / PUE
            node_name = energy_metric.node
            node_context = node_contexts.get(node_name)
            emaps_zone = node_context.emaps_zone if node_context else self._config.DEFAULT_ZONE
            provider = nodes_info.get(node_name).cloud_provider if nodes_info.get(node_name) else None
            pue = self._config.get_pue_for_provider(provider)

            # Estimation flags
            is_estimated, estimation_reasons = self.build_estimation_flags(
                energy_metric=energy_metric,
                node_context=node_context,
                cost_metric=cost_metric,
                provider=provider,
                pue=pue,
                node_name=node_name,
                cpu_adjusted_nodes=cpu_adjusted_nodes,
            )

            # Embodied emissions
            embodied_emissions_grams = self.embodied_service.calculate_pod_embodied(
                node_info=nodes_info.get(node_name),
                boavizta_cache=boavizta_cache,
                pod_requests=pod_requests,
            )

            # Carbon calculation
            try:
                carbon_result = await self.calculator.calculate_emissions(
                    joules=energy_metric.joules,
                    zone=emaps_zone,
                    timestamp=energy_metric.timestamp,
                    pue=pue,
                )
            except Exception as e:
                logger.error(
                    "Failed to calculate emissions for pod '%s': %s",
                    pod_name,
                    e,
                )
                carbon_result = None

            if carbon_result:
                # Sanity checks: guard against obviously wrong data
                final_joules = energy_metric.joules
                final_co2 = carbon_result.co2e_grams
                if final_joules < 0:
                    logger.warning(
                        "Negative energy for pod '%s': %.1f J — clamped to 0.",
                        pod_name,
                        final_joules,
                    )
                    final_joules = 0.0
                    final_co2 = 0.0
                if final_co2 > 10000:
                    logger.warning(
                        "Unusually high CO2 for pod '%s': %.1f g in one step. Check node profile and grid intensity.",
                        pod_name,
                        final_co2,
                    )

                combined = CombinedMetric(
                    pod_name=pod_name,
                    namespace=namespace,
                    total_cost=total_cost,
                    co2e_grams=final_co2,
                    pue=pue,
                    grid_intensity=carbon_result.grid_intensity,
                    joules=final_joules,
                    cpu_request=pod_requests["cpu"],
                    memory_request=pod_requests["memory"],
                    cpu_usage_millicores=resource_maps.cpu_usage_map.get(pod_key),
                    memory_usage_bytes=resource_maps.memory_usage_map.get(pod_key),
                    network_receive_bytes=resource_maps.network_rx_map.get(pod_key),
                    network_transmit_bytes=resource_maps.network_tx_map.get(pod_key),
                    disk_read_bytes=resource_maps.disk_read_map.get(pod_key),
                    disk_write_bytes=resource_maps.disk_write_map.get(pod_key),
                    ephemeral_storage_request_bytes=pod_requests.get("ephemeral_storage"),
                    restart_count=resource_maps.restart_map.get(pod_key),
                    owner_kind=pod_requests.get("owner_kind"),
                    owner_name=pod_requests.get("owner_name"),
                    timestamp=energy_metric.timestamp,
                    duration_seconds=self.estimator.query_range_step_sec,
                    grid_intensity_timestamp=carbon_result.grid_intensity_timestamp,
                    node=node_name,
                    node_instance_type=(
                        nodes_info.get(node_name).instance_type
                        if nodes_info.get(node_name)
                        else node_instance_map.get(node_name)
                    ),
                    node_zone=(nodes_info.get(node_name).zone if nodes_info.get(node_name) else None),
                    emaps_zone=emaps_zone,
                    is_estimated=is_estimated,
                    estimation_reasons=estimation_reasons,
                    embodied_co2e_grams=embodied_emissions_grams,
                    calculation_version=__version__,
                )
                combined_metrics.append(combined)
            else:
                logger.info(
                    "Skipping combined metric for pod '%s' due to calculation error.",
                    pod_name,
                )

        return combined_metrics
