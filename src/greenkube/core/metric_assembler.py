# src/greenkube/core/metric_assembler.py
"""Assembles CombinedMetric objects from collected data."""

import logging
from typing import Any, Dict, List, Optional, Sequence, Set

from ..collectors.base_electricity_provider import BaseElectricityProvider
from ..core.calculator import CarbonCalculator
from ..core.config import Config, get_config
from ..core.embodied_service import EmbodiedEmissionsService
from ..core.node_zone_mapper import NodeZoneMapper
from ..core.prometheus_resource_mapper import PodResourceMaps
from ..energy.estimator import BasicEstimator
from ..impact.rows import container_gpu_fields
from ..models.metrics import CombinedMetric, CostMetric, EnergyMetric, PodMetric
from ..models.node import NodeZoneContext
from ..models.provenance import (
    INTENSITY_FALLBACK_REASON_PREFIX,
    CostSource,
    IntensitySource,
    PowerSource,
    UsageSource,
    cost_source_for,
)
from ..storage.base_repository import CarbonIntensityRepository
from ..storage.container_rows import CONTAINER_SAMPLE_INCOMPLETE as _CONTAINER_SAMPLE_INCOMPLETE

logger = logging.getLogger(__name__)

#: Re-exported: estimation reason of container rows of a pod where some containers had no sample (COL-17).
CONTAINER_SAMPLE_INCOMPLETE = _CONTAINER_SAMPLE_INCOMPLETE


#: Intensity provenance values that mean the figure is not a fresh provider value.
_DEFAULTED_INTENSITY = {
    IntensitySource.ZONE_DEFAULT.value: "zone default",
    IntensitySource.GLOBAL_DEFAULT.value: "global default (DEFAULT_INTENSITY)",
    IntensitySource.STALE.value: "stale stored value (older than INTENSITY_MAX_AGE)",
}


def cost_fields(cost: Optional[CostMetric], share: float, default_cost: float) -> Dict[str, Any]:
    """Return the cost columns of a row from an allocation and the share of it the row covers.

    An allocation whose cost the collector found untrusted (``source = none``:
    NaN, missing, or a zero total for a running pod with requests) is treated
    as no allocation: the row gets the default placeholder with
    ``cost_source = none``, never a trusted zero.
    """
    if cost is None or cost.source == CostSource.NONE.value:
        return {
            "total_cost": default_cost,
            "cost_cpu": None,
            "cost_ram": None,
            "cost_storage": None,
            "cost_network": None,
            "cost_lb": None,
            "cost_source": cost_source_for(False).value,
        }
    return {
        "total_cost": cost.total_cost * share,
        "cost_cpu": cost.cpu_cost * share,
        "cost_ram": cost.ram_cost * share,
        "cost_storage": cost.storage_cost * share,
        "cost_network": cost.network_cost * share,
        "cost_lb": cost.lb_cost * share,
        "cost_source": cost.source or cost_source_for(True).value,
    }


def power_source_for_profile(estimator: Any, profile: Any) -> str:
    """Classify the power profile an energy estimate used (``power_source``).

    A resolved :class:`~greenkube.energy.node_power.NodePowerProfile` already
    carries its provenance; raw profile dicts are classified by identity.
    """
    resolved_source = getattr(profile, "source", None)
    if isinstance(resolved_source, str):
        return resolved_source
    if profile is None or profile is getattr(estimator, "DEFAULT_INSTANCE_PROFILE", None):
        return PowerSource.DEFAULT_PROFILE.value
    profiles = getattr(estimator, "instance_profiles", None) or {}
    if any(profile is candidate for candidate in profiles.values()):
        return PowerSource.INSTANCE_PROFILE.value
    return PowerSource.NODE_CAPACITY_DERIVED.value


def provenance_estimation_reasons(intensity_source: Optional[str], power_source: Optional[str]) -> List[str]:
    """Explain intensity and power fallbacks in the legacy ``estimation_reasons`` list."""
    reasons: List[str] = []
    if intensity_source in _DEFAULTED_INTENSITY:
        reasons.append(f"{INTENSITY_FALLBACK_REASON_PREFIX}{_DEFAULTED_INTENSITY[intensity_source]}")
    if power_source == PowerSource.DEFAULT_PROFILE.value:
        reasons.append("Node power profile unknown; used the default instance profile")
    return reasons


#: Estimation reason of a container row whose usage is a Metrics API point sample (about 15-30 s, read at
#: collection time), not a step average (COL-05).
METRICS_API_POINT_SAMPLE = "metrics_api_point_sample"


def _container_usage_source(resource_maps: PodResourceMaps, container_key: tuple, aggregate: CombinedMetric) -> str:
    """Usage source of a container row: its own when it differs from the pod row's (COL-05)."""
    source = resource_maps.container_usage_source_map.get(container_key)
    if source:
        return source
    if container_key in resource_maps.container_timestamp_map:
        return UsageSource.METRICS_SERVER.value
    return aggregate.usage_source or UsageSource.PROMETHEUS.value


def _container_reasons(reasons: List[str], usage_source: str) -> List[str]:
    if usage_source == UsageSource.METRICS_SERVER.value:
        return list(dict.fromkeys([*reasons, METRICS_API_POINT_SAMPLE]))
    return list(reasons)


def _weights(values: Sequence[Optional[float]]) -> Optional[List[float]]:
    if any(value is None for value in values):
        return None
    weights = [max(float(value or 0), 0.0) for value in values]
    return weights if sum(weights) > 0 else None


class MetricAssembler:
    """Shared row-building helpers for the range collection path.

    Zone mapping is handled by :class:`NodeZoneMapper` and embodied emissions
    by :class:`EmbodiedEmissionsService`.
    """

    def __init__(
        self,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
        repository: CarbonIntensityRepository,
        electricity_provider: BaseElectricityProvider,
        zone_mapper: NodeZoneMapper,
        embodied_service: EmbodiedEmissionsService,
        config: Config | None = None,
    ):
        self.calculator = calculator
        self.estimator = estimator
        self.repository = repository
        self.electricity_provider = electricity_provider
        self.zone_mapper = zone_mapper
        self.embodied_service = embodied_service
        self._config = config if config is not None else get_config()

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

        cost_total = getattr(cost_metric, "total_cost", None) if cost_metric else None
        if cost_metric is not None and getattr(cost_metric, "source", None) == CostSource.NONE.value:
            is_estimated = True
            estimation_reasons.append(
                f"OpenCost cost of pod '{energy_metric.pod_name}' is missing or not finite. "
                f"Used default cost {self._config.DEFAULT_COST}"
            )
        elif not cost_metric:
            is_estimated = True
            estimation_reasons.append(
                f"No cost data for pod '{energy_metric.pod_name}'. Used default cost {self._config.DEFAULT_COST}"
            )
        elif isinstance(cost_total, (int, float)) and cost_total < 0:
            is_estimated = True
            estimation_reasons.append("OpenCost returned negative pod cost; cost-based savings are not trusted")

        if not provider:
            is_estimated = True
            estimation_reasons.append(f"Unknown provider for node '{node_name}'. Used default PUE {pue}")
        elif f"default_{provider.lower()}" not in self._config.DATACENTER_PUE_PROFILES:
            is_estimated = True
            estimation_reasons.append(f"No PUE profile for provider '{provider}'. Used default PUE {pue}")

        if node_name in cpu_adjusted_nodes:
            is_estimated = True
            estimation_reasons.append(f"CPU usage on node '{node_name}' was below threshold; substituted pod requests")

        return is_estimated, list(dict.fromkeys(estimation_reasons))

    @staticmethod
    def assemble_container_metrics(
        combined_metrics: Sequence[CombinedMetric],
        pod_metrics: Sequence[PodMetric],
        resource_maps: PodResourceMaps,
    ) -> List[CombinedMetric]:
        """Build container-scoped records while leaving pod aggregates unchanged.

        Container rows inherit the provenance of their pod aggregate (A11): an
        OpenCost-priced, Prometheus-measured pod yields trusted container rows,
        not blanket "estimated" ones (BUG-29). CPU cost follows the CPU share,
        RAM cost follows the memory share; classic init containers get no row.
        """
        containers_by_pod: dict[tuple[str, str], list[PodMetric]] = {}
        for pod_metric in pod_metrics:
            if not pod_metric.container_name or pod_metric.container_name.strip() != pod_metric.container_name:
                continue
            if pod_metric.kind == "init":
                continue
            key = (pod_metric.namespace, pod_metric.pod_name)
            containers_by_pod.setdefault(key, []).append(pod_metric)

        container_metrics: list[CombinedMetric] = []
        for aggregate in combined_metrics:
            if not aggregate.namespace or not aggregate.pod_name or aggregate.container_name:
                continue
            key = (aggregate.namespace, aggregate.pod_name)
            containers = containers_by_pod.get(key, [])
            if not containers:
                continue

            # A container without a sample at the step (waiting, finished, just restarted) gets no
            # row, and no longer drops the rows of its siblings (COL-17): the sampled containers carry
            # the whole pod so container rows still sum to the pod row, flagged as incomplete.
            sampled = [
                item
                for item in containers
                if (aggregate.namespace, aggregate.pod_name, item.container_name)
                in resource_maps.container_cpu_usage_map
                or (aggregate.namespace, aggregate.pod_name, item.container_name)
                in resource_maps.container_memory_usage_map
            ]
            if not sampled:
                continue
            incomplete = len(sampled) < len(containers)
            containers = sampled
            container_keys = [(aggregate.namespace, aggregate.pod_name, item.container_name) for item in containers]
            estimation_reasons = list(aggregate.estimation_reasons)
            if incomplete:
                estimation_reasons = list(dict.fromkeys([*estimation_reasons, CONTAINER_SAMPLE_INCOMPLETE]))

            cpu_weights = (
                [max(resource_maps.container_cpu_usage_map[key], 0) for key in container_keys]
                if all(key in resource_maps.container_cpu_usage_map for key in container_keys)
                else []
            )
            if not cpu_weights or sum(cpu_weights) == 0:
                memory_weights = (
                    [max(resource_maps.container_memory_usage_map[key], 0) for key in container_keys]
                    if all(key in resource_maps.container_memory_usage_map for key in container_keys)
                    else []
                )
                if memory_weights and sum(memory_weights) > 0:
                    cpu_weights = memory_weights
            if not cpu_weights or sum(cpu_weights) == 0:
                cpu_weights = [max(item.cpu_request, 0) for item in containers]
            if sum(cpu_weights) == 0:
                cpu_weights = [max(item.memory_request, 0) for item in containers]
            if sum(cpu_weights) == 0:
                cpu_weights = [1] * len(containers)
            total_weight = sum(cpu_weights)
            memory_weights = (
                _weights([resource_maps.container_memory_usage_map.get(key) for key in container_keys])
                or _weights([item.memory_request for item in containers])
                or cpu_weights
            )
            total_memory_weight = sum(memory_weights)
            has_components = aggregate.cost_cpu is not None or aggregate.cost_ram is not None
            gpu_fields = container_gpu_fields(aggregate, len(containers))

            for pod_metric, container_key, weight, memory_weight in zip(
                containers, container_keys, cpu_weights, memory_weights, strict=True
            ):
                share = weight / total_weight
                memory_share = memory_weight / total_memory_weight
                usage_source = _container_usage_source(resource_maps, container_key, aggregate)
                if has_components:
                    cost_cpu = (aggregate.cost_cpu or 0.0) * share
                    cost_ram = (aggregate.cost_ram or 0.0) * memory_share
                    other_cost = aggregate.total_cost - (aggregate.cost_cpu or 0.0) - (aggregate.cost_ram or 0.0)
                    total_cost = cost_cpu + cost_ram + other_cost * share
                else:
                    cost_cpu = cost_ram = None
                    total_cost = aggregate.total_cost * share
                container_metrics.append(
                    aggregate.model_copy(
                        update={
                            "container_name": pod_metric.container_name,
                            "total_cost": total_cost,
                            "cost_cpu": cost_cpu,
                            "cost_ram": cost_ram,
                            "cost_storage": None if aggregate.cost_storage is None else aggregate.cost_storage * share,
                            "cost_network": None if aggregate.cost_network is None else aggregate.cost_network * share,
                            "cost_lb": None if aggregate.cost_lb is None else aggregate.cost_lb * share,
                            "usage_source": usage_source,
                            **gpu_fields,
                            "cpu_limit": pod_metric.cpu_limit,
                            "memory_limit": pod_metric.memory_limit,
                            "co2e_grams": aggregate.co2e_grams * share,
                            "embodied_co2e_grams": (aggregate.embodied_co2e_grams or 0.0) * share,
                            "joules": aggregate.joules * share,
                            "cpu_request": pod_metric.cpu_request,
                            "memory_request": pod_metric.memory_request,
                            "cpu_usage_millicores": resource_maps.container_cpu_usage_map.get(container_key),
                            "cpu_usage_max_millicores": None,
                            "memory_usage_bytes": resource_maps.container_memory_usage_map.get(container_key),
                            "memory_usage_max_bytes": None,
                            "network_receive_bytes": None,
                            "network_transmit_bytes": None,
                            "disk_read_bytes": None,
                            "disk_write_bytes": None,
                            "storage_request_bytes": None,
                            "storage_usage_bytes": None,
                            "ephemeral_storage_request_bytes": pod_metric.ephemeral_storage_request,
                            "ephemeral_storage_usage_bytes": None,
                            "restart_count": resource_maps.container_restart_map.get(container_key),
                            "owner_kind": pod_metric.owner_kind or aggregate.owner_kind,
                            "owner_name": pod_metric.owner_name or aggregate.owner_name,
                            "timestamp": resource_maps.container_timestamp_map.get(container_key)
                            or aggregate.timestamp,
                            "duration_seconds": resource_maps.container_duration_seconds_map.get(container_key)
                            or aggregate.duration_seconds,
                            "sample_count": resource_maps.container_sample_count_map.get(
                                container_key, aggregate.sample_count
                            ),
                            "is_estimated": aggregate.is_estimated
                            or incomplete
                            or usage_source == UsageSource.METRICS_SERVER.value,
                            "estimation_reasons": _container_reasons(estimation_reasons, usage_source),
                        }
                    )
                )
        return container_metrics
