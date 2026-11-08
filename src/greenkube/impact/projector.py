# src/greenkube/impact/projector.py
"""Bridges GreenKube's stored metrics to the Impact Model.

Analyzers and connectors call :class:`ImpactProjector` instead of computing
savings themselves (roadmap B4). The projector turns ``CombinedMetric`` rows
and ``NodeInfo`` snapshots into model inputs (samples, node profiles, unit
prices) and delegates every formula to :class:`ImpactModel`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from greenkube.impact.model import DEFAULT_IMPACT_MODEL, ImpactModel
from greenkube.impact.power import memory_watts_per_gib
from greenkube.impact.pricing import (
    CustomRates,
    PricingCatalog,
    UnitPriceResolver,
    observed_rates_from_rows,
    row_hours,
)
from greenkube.impact.realization import (
    SOURCE_CONFIG,
    SOURCE_CONTEXT,
    SOURCE_UNOBSERVED,
    UNOBSERVED_FACTOR,
    current_realization_context,
    realization_override,
)
from greenkube.impact.types import (
    BYTES_PER_GIB,
    HOURS_PER_YEAR,
    AssetDeletion,
    ContainerResourceChange,
    CostSource,
    ImpactDelta,
    NodeImpactProfile,
    NodePoolChange,
    NodePoolContext,
    PowerSource,
    ReplicaScheduleChange,
    ResourceSample,
    WorkloadBaseline,
    WorkloadDeletion,
    WorkloadRef,
)
from greenkube.utils.time import parse_query_step_seconds

logger = logging.getLogger(__name__)

#: Attribute used to cache one projector per optimization context.
_CONTEXT_CACHE_ATTR = "_greenkube_impact_projector"

#: Name of the fallback node profile used for samples on unknown nodes.
UNKNOWN_NODE = "__unknown__"

#: Fallbacks used only when the configuration object lacks a setting.
_FALLBACK_LIFESPAN_YEARS = 4
_FALLBACK_EMBODIED_KG = 100.0
_FALLBACK_INTENSITY = 500.0
_FALLBACK_PUE = 1.3


def _setting(config: Any, name: str, default: Any) -> Any:
    value = getattr(config, name, default)
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
    elif isinstance(default, str) and not isinstance(value, str):
        return default
    return value


def _positive(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


@dataclass(frozen=True)
class _PowerProfile:
    vcores: float
    min_watts: float
    max_watts: float
    source: PowerSource


def _power_profile(config: Any, instance_type: Optional[str], capacity_cores: Optional[float]) -> _PowerProfile:
    """Mirrors ``BasicEstimator`` profile selection: instance profile, capacity-derived, default."""
    from greenkube.data.instance_profiles import INSTANCE_PROFILES

    profile = INSTANCE_PROFILES.get(instance_type) if instance_type else None
    if profile:
        vcores = float(profile.get("vcores") or 0) or (capacity_cores or 1.0)
        return _PowerProfile(
            vcores=vcores,
            min_watts=float(profile.get("minWatts", 0.0)),
            max_watts=float(profile.get("maxWatts", 0.0)),
            source=PowerSource.INSTANCE_PROFILE,
        )

    default_vcores = float(_setting(config, "DEFAULT_INSTANCE_VCORES", 1) or 1)
    default_min = float(_setting(config, "DEFAULT_INSTANCE_MIN_WATTS", 1.0))
    default_max = float(_setting(config, "DEFAULT_INSTANCE_MAX_WATTS", 10.0))
    if capacity_cores:
        per_core_min = default_min / default_vcores if default_vcores > 0 else default_min
        per_core_max = default_max / default_vcores if default_vcores > 0 else default_max
        return _PowerProfile(
            vcores=capacity_cores,
            min_watts=per_core_min * capacity_cores,
            max_watts=per_core_max * capacity_cores,
            source=PowerSource.NODE_CAPACITY_DERIVED,
        )
    return _PowerProfile(
        vcores=default_vcores,
        min_watts=default_min,
        max_watts=default_max,
        source=PowerSource.DEFAULT_PROFILE,
    )


def _provider_pue(config: Any, provider: Optional[str]) -> float:
    getter = getattr(config, "get_pue_for_provider", None)
    if callable(getter):
        try:
            value = getter(provider)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
        except Exception:  # pragma: no cover - defensive against custom configs
            logger.debug("Could not resolve PUE for provider %s", provider)
    return float(_setting(config, "DEFAULT_PUE", _FALLBACK_PUE))


def sample_from_metric(metric: Any, ref: WorkloadRef, default_hours: float) -> ResourceSample:
    """Converts a ``CombinedMetric`` row into a model sample."""

    def _cores(value: Any) -> Optional[float]:
        return float(value) / 1000.0 if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _gib(value: Any) -> Optional[float]:
        return float(value) / BYTES_PER_GIB if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _cost(name: str) -> Optional[float]:
        value = getattr(metric, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    total_cost = _cost("total_cost")
    return ResourceSample(
        ref=ref,
        hours=row_hours(metric, default_hours),
        cpu_request_cores=max(_cores(getattr(metric, "cpu_request", 0)) or 0.0, 0.0),
        cpu_usage_cores=_cores(getattr(metric, "cpu_usage_millicores", None)),
        mem_request_gib=max(_gib(getattr(metric, "memory_request", 0)) or 0.0, 0.0),
        mem_usage_gib=_gib(getattr(metric, "memory_usage_bytes", None)),
        node=getattr(metric, "node", None),
        timestamp=getattr(metric, "timestamp", None),
        pue=_positive(getattr(metric, "pue", None)),
        grid_intensity_g_per_kwh=_positive(getattr(metric, "grid_intensity", None)),
        observed_cost_total=total_cost if total_cost is not None and total_cost > 0 else None,
        observed_cost_cpu=_cost("cost_cpu"),
        observed_cost_ram=_cost("cost_ram"),
        observed_cost_storage=_cost("cost_storage"),
        observed_cost_network=_cost("cost_network"),
        observed_cost_lb=_cost("cost_lb"),
        observed_cost_source=getattr(metric, "cost_source", None),
        gpu_watts=_positive(getattr(metric, "gpu_power_watts", None)),
    )


def observed_window_hours(samples: Sequence[ResourceSample]) -> float:
    """Observation span of samples when the analysis window is unknown."""
    timestamped = [sample for sample in samples if sample.timestamp is not None]
    if timestamped:
        start = min(sample.timestamp for sample in timestamped if sample.timestamp is not None)
        end = max(
            sample.timestamp + timedelta(hours=sample.hours) for sample in timestamped if sample.timestamp is not None
        )
        span = (end - start).total_seconds() / 3600.0
        if span > 0:
            return span
    return sum(max(sample.hours, 0.0) for sample in samples)


class ImpactProjector:
    """Per-scan entry point of the Impact Model for analyzers and connectors."""

    def __init__(
        self,
        config: Any,
        *,
        node_infos: Iterable[Any] = (),
        rows: Iterable[Any] = (),
        analysis_window_seconds: Optional[float] = None,
        model: Optional[ImpactModel] = None,
        catalog: Optional[PricingCatalog] = None,
    ):
        self.config = config
        self.model: ImpactModel = model if model is not None else DEFAULT_IMPACT_MODEL
        self.cluster_uid = str(_setting(config, "CLUSTER_NAME", "") or "")
        step = _setting(config, "PROMETHEUS_QUERY_RANGE_STEP", "5m")
        self.default_hours = parse_query_step_seconds(step) / 3600.0
        self.window_hours = (
            float(analysis_window_seconds) / 3600.0
            if isinstance(analysis_window_seconds, (int, float)) and analysis_window_seconds > 0
            else None
        )
        rows = list(rows)
        self.resolver = UnitPriceResolver(
            catalog=catalog if catalog is not None else PricingCatalog(),
            custom_rates=CustomRates.from_config(config),
            observed=observed_rates_from_rows(rows, self.default_hours),
        )
        self.pool = self._build_pool(list(node_infos or []), rows)

    # ------------------------------------------------------------------ #
    # Construction                                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    def for_context(cls, context: Any) -> "ImpactProjector":
        """Returns the projector of an optimization context, built once per scan."""
        cached = getattr(context, _CONTEXT_CACHE_ATTR, None)
        if isinstance(cached, ImpactProjector):
            return cached
        rows = list(getattr(context, "metrics", None) or []) + list(getattr(context, "container_metrics", None) or [])
        projector = cls(
            context.config,
            node_infos=getattr(context, "node_infos", None) or [],
            rows=rows,
            analysis_window_seconds=getattr(context, "analysis_window_seconds", None),
        )
        try:
            setattr(context, _CONTEXT_CACHE_ATTR, projector)
        except (AttributeError, TypeError):  # pragma: no cover - frozen or slotted contexts
            pass
        return projector

    def _build_pool(self, node_infos: List[Any], rows: List[Any]) -> NodePoolContext:
        config = self.config
        default_intensity = float(_setting(config, "DEFAULT_INTENSITY", _FALLBACK_INTENSITY))
        env = _row_environment(rows)
        nodes: Dict[str, NodeImpactProfile] = {}
        for info in node_infos:
            name = getattr(info, "name", None)
            if not isinstance(name, str) or not name:
                continue
            nodes[name] = self._node_profile(
                name=name,
                instance_type=getattr(info, "instance_type", None),
                provider=getattr(info, "cloud_provider", None),
                region=getattr(info, "region", None),
                capacity_cores=_positive(getattr(info, "cpu_capacity_cores", None)),
                memory_bytes=_positive(getattr(info, "memory_capacity_bytes", None)),
                embodied_kg=_positive(getattr(info, "embodied_emissions_kg", None)),
                env=env.get(name),
                default_intensity=default_intensity,
            )

        # Nodes seen in metric rows but absent from the node snapshot keep their
        # instance type (power profile) and observed prices, if any.
        for name, instance_type in _row_instance_types(rows).items():
            if name in nodes:
                continue
            nodes[name] = self._node_profile(
                name=name,
                instance_type=instance_type,
                provider=None,
                region=None,
                capacity_cores=None,
                memory_bytes=None,
                embodied_kg=None,
                env=env.get(name),
                default_intensity=default_intensity,
            )

        default_node = self._node_profile(
            name=UNKNOWN_NODE,
            instance_type=None,
            provider=None,
            region=None,
            capacity_cores=None,
            memory_bytes=None,
            embodied_kg=None,
            env=None,
            default_intensity=default_intensity,
            is_fallback=True,
        )
        override = realization_override(config)
        context = current_realization_context() if override is None else None
        if override is not None:
            factor, source, reasons = override, SOURCE_CONFIG, ("IMPACT_REALIZATION_FACTOR pins the factor",)
        elif context is not None:
            default_pool = context.pools[context.default_pool]
            factor, source, reasons = default_pool.factor, SOURCE_CONTEXT, default_pool.reasons
        else:
            factor, source = UNOBSERVED_FACTOR, SOURCE_UNOBSERVED
            reasons = ("The cluster's autoscalers have not been observed yet: expected savings equal allocated",)
        return NodePoolContext(
            nodes=nodes,
            default_node=default_node,
            realization_factor=factor,
            carbon_price_per_tonne=float(_setting(config, "CARBON_PRICE_PER_TONNE", 100.0)),
            currency=self.resolver.custom_rates.currency,
            realization_source=source,
            realization_reasons=tuple(reasons),
            realization=context,
        )

    def _node_profile(
        self,
        *,
        name: str,
        instance_type: Optional[str],
        provider: Optional[str],
        region: Optional[str],
        capacity_cores: Optional[float],
        memory_bytes: Optional[float],
        embodied_kg: Optional[float],
        env: Optional[Tuple[float, float]],
        default_intensity: float,
        is_fallback: bool = False,
    ) -> NodeImpactProfile:
        config = self.config
        power = _power_profile(config, instance_type, capacity_cores)
        cores = capacity_cores or power.vcores
        memory_gib = memory_bytes / BYTES_PER_GIB if memory_bytes else 0.0
        prices = self.resolver.resolve(
            node_name=None if is_fallback else name,
            provider=provider,
            region=region,
            instance_type=instance_type,
            vcpus=cores,
            memory_gib=memory_gib or None,
        )
        if not memory_gib and prices.source == CostSource.PRICING_CATALOG:
            entry = self.resolver.catalog.lookup(provider, region, instance_type)
            memory_gib = entry.memory_gib if entry is not None and entry.memory_gib else 0.0

        lifespan_years = float(_setting(config, "DEFAULT_HARDWARE_LIFESPAN_YEARS", _FALLBACK_LIFESPAN_YEARS))
        lifespan_hours = max(lifespan_years, 0.0) * HOURS_PER_YEAR
        gwp_kg = embodied_kg or float(_setting(config, "DEFAULT_EMBODIED_EMISSIONS_KG", _FALLBACK_EMBODIED_KG))
        embodied_g_per_hour = gwp_kg * 1000.0 / lifespan_hours if lifespan_hours > 0 else 0.0

        pue = env[0] if env else _provider_pue(config, provider)
        intensity = env[1] if env and env[1] > 0 else default_intensity
        return NodeImpactProfile(
            name=name,
            cpu_capacity_cores=cores,
            mem_capacity_gib=memory_gib,
            idle_watts=power.min_watts,
            max_watts=power.max_watts,
            embodied_g_per_hour=embodied_g_per_hour,
            prices=prices,
            pue=pue,
            grid_intensity_g_per_kwh=intensity,
            power_source=power.source,
            instance_type=instance_type,
            region=region,
            provider=provider,
            is_fallback=is_fallback,
            memory_watts_per_gib=memory_watts_per_gib(config),
        )

    # ------------------------------------------------------------------ #
    # Baselines                                                           #
    # ------------------------------------------------------------------ #

    def workload_ref(
        self,
        namespace: Optional[str],
        kind: str,
        name: str,
        container: Optional[str] = None,
    ) -> WorkloadRef:
        return WorkloadRef(
            cluster_uid=self.cluster_uid,
            namespace=namespace or "",
            kind=kind,
            name=name,
            container=container,
        )

    def baseline(self, ref: WorkloadRef, series: Sequence[Any]) -> WorkloadBaseline:
        samples = [sample_from_metric(metric, ref, self.default_hours) for metric in series]
        window = self.window_hours if self.window_hours is not None else observed_window_hours(samples)
        return WorkloadBaseline(ref=ref, samples=samples, window_hours=window)

    # ------------------------------------------------------------------ #
    # Projections                                                         #
    # ------------------------------------------------------------------ #

    def container_resources(
        self,
        ref: WorkloadRef,
        series: Sequence[Any],
        *,
        cpu_millicores: Optional[Tuple[float, float]] = None,
        memory_bytes: Optional[Tuple[float, float]] = None,
    ) -> ImpactDelta:
        """Projects a request change ``(current, proposed)`` for one container."""
        change = ContainerResourceChange(
            cpu_request_cores=(cpu_millicores[0] / 1000.0, cpu_millicores[1] / 1000.0) if cpu_millicores else None,
            mem_request_gib=(
                (memory_bytes[0] / BYTES_PER_GIB, memory_bytes[1] / BYTES_PER_GIB) if memory_bytes else None
            ),
        )
        return self.model.project(self.baseline(ref, series), change, self.pool)

    def workload_deletion(self, ref: WorkloadRef, series: Sequence[Any]) -> ImpactDelta:
        """Projects removing every replica of a workload."""
        return self.model.project(self.baseline(ref, series), WorkloadDeletion(), self.pool)

    def replica_schedule(
        self, ref: WorkloadRef, series: Sequence[Any], removed_hours_of_day: Iterable[int]
    ) -> ImpactDelta:
        """Projects scaling a workload to zero during the given UTC hours."""
        change = ReplicaScheduleChange(removed_hours_of_day=frozenset(int(hour) % 24 for hour in removed_hours_of_day))
        return self.model.project(self.baseline(ref, series), change, self.pool)

    def asset_deletion(
        self,
        *,
        asset_kind: str,
        allocated_cost_annual: float,
        component: str,
        cost_source: CostSource,
        detail: str = "",
    ) -> ImpactDelta:
        """Projects deleting a non-compute asset with a known allocated annual cost."""
        ref = self.workload_ref(None, asset_kind, asset_kind)
        change = AssetDeletion(
            asset_kind=asset_kind,
            allocated_cost_annual=allocated_cost_annual,
            component=component,
            cost_source=cost_source,
            detail=detail,
        )
        return self.model.project(WorkloadBaseline(ref=ref, samples=(), window_hours=HOURS_PER_YEAR), change, self.pool)

    def node_removal(self, node_names: Sequence[str], *, action_is_consolidation: bool = True) -> ImpactDelta:
        """Projects removing whole nodes (consolidation)."""
        ref = self.workload_ref(None, "NodePool", ",".join(node_names))
        change = NodePoolChange(removed_nodes=tuple(node_names), action_is_consolidation=action_is_consolidation)
        return self.model.project(WorkloadBaseline(ref=ref, samples=(), window_hours=HOURS_PER_YEAR), change, self.pool)

    def node_hourly_price(self, node_name: str) -> Optional[float]:
        """Full hourly price of a known node, from its resolved unit prices."""
        node = self.pool.nodes.get(node_name)
        return node.hourly_price if node is not None else None


def _row_environment(rows: Iterable[Any]) -> Mapping[str, Tuple[float, float]]:
    """Average PUE and grid intensity observed per node, weighted by row count."""
    sums: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for row in rows:
        node = getattr(row, "node", None)
        if not node:
            continue
        pue = _positive(getattr(row, "pue", None))
        intensity = _positive(getattr(row, "grid_intensity", None))
        acc = sums[node]
        if pue is not None:
            acc[0] += pue
            acc[1] += 1
        if intensity is not None:
            acc[2] += intensity
            acc[3] += 1
    environment: Dict[str, Tuple[float, float]] = {}
    for node, (pue_sum, pue_n, intensity_sum, intensity_n) in sums.items():
        if pue_n and intensity_n:
            environment[node] = (pue_sum / pue_n, intensity_sum / intensity_n)
    return environment


def _row_instance_types(rows: Iterable[Any]) -> Mapping[str, Optional[str]]:
    instance_types: Dict[str, Optional[str]] = {}
    for row in rows:
        node = getattr(row, "node", None)
        if not node:
            continue
        instance_type = getattr(row, "node_instance_type", None)
        if node not in instance_types or (instance_type and not instance_types[node]):
            instance_types[node] = instance_type if isinstance(instance_type, str) and instance_type else None
    return instance_types


def impact_fields(delta: ImpactDelta) -> Dict[str, Any]:
    """Recommendation fields derived from an impact delta (API v1 compatibility).

    ``potential_savings_cost`` is the expected billable saving and
    ``potential_savings_co2e_grams`` the expected CO2e saving; CO2e stays None
    when the change is outside the energy model (storage, load balancers).
    """
    return {
        "potential_savings_cost": delta.expected_billable_cost_annual,
        "potential_savings_co2e_grams": delta.co2e_expected_annual_g if delta.co2e_modeled else None,
        "impact": delta.to_dict(),
    }
