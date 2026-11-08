# src/greenkube/impact/rows.py
"""Allocation of stored metric rows by the Impact Model (roadmap B2, B8).

Collection (``HistoricalRangeProcessor``) and the ``greenkube recompute``
command both turn the pod rows of one node and one interval into
:class:`~greenkube.impact.types.ResourceSample` values, run
:meth:`ImpactModel.allocate` on them and write the result back:

* ``joules``, ``co2e_grams`` and ``embodied_co2e_grams`` become the
  reservation-aware allocation (idle power and embodied emissions by
  reservation, dynamic power by usage, memory power by memory reservation,
  GPU power by DCGM attribution);
* rows without an observed (OpenCost/Kubecost) cost receive the modeled cost
  of their reservation at the node's unit prices, labeled with the price tier
  (``pricing_catalog`` or ``custom_rate``); observed costs are kept;
* ``model_version`` records the model that produced the numbers;
* the unreserved remainder of the node becomes one :class:`NodeIdleMetric`.

The sum of the allocated rows and the idle row equals the node totals for
energy, operational and embodied CO2e (and for cost when no row carries an
observed cost).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from greenkube.impact.model import DEFAULT_IMPACT_MODEL, ImpactModel
from greenkube.impact.power import memory_watts_per_gib
from greenkube.impact.pricing import CustomRates, PricingCatalog, UnitPriceResolver, observed_rates_from_rows
from greenkube.impact.projector import sample_from_metric
from greenkube.impact.types import (
    BYTES_PER_GIB,
    IMPACT_MODEL_VERSION,
    ClusterSnapshot,
    NodeImpactProfile,
    PowerSource,
    UnitPrices,
    WorkloadImpact,
    WorkloadRef,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.models.provenance import ALLOCATED_COST_SOURCES, CostSource

#: Joules per kWh.
JOULES_PER_KWH = 3.6e6

#: Scope of the idle rows (roadmap B2).
IDLE_SCOPE = "idle"

#: Synthetic node of rows whose node could not be resolved (no idle row is written for it).
UNRESOLVED_NODE = "__unresolved__"

#: ``estimation_reasons`` prefix written when a row's cost is modeled, not observed.
MODELED_COST_REASON_PREFIX = "No observed cost for pod"


@dataclass
class NodeIdleMetric:
    """Unreserved capacity of one node over one interval (the ``__idle__`` workload).

    Stored in ``node_idle_metrics``; never mixed into pod rows, so analyzers
    and pod-level dashboards are unaffected, while node totals stay
    reconstructible as ``Σ pod rows + idle row``.
    """

    node: str
    timestamp: datetime
    duration_seconds: int
    joules: float
    co2e_grams: float
    embodied_co2e_grams: float
    total_cost: float
    cost_cpu: float
    cost_ram: float
    cost_source: Optional[str]
    cpu_unreserved_cores: float
    memory_unreserved_bytes: float
    gpu_power_watts: Optional[float] = None
    pue: Optional[float] = None
    grid_intensity: Optional[float] = None
    emaps_zone: Optional[str] = None
    power_source: Optional[str] = None
    intensity_source: Optional[str] = None
    model_version: str = IMPACT_MODEL_VERSION
    scope: str = IDLE_SCOPE


@dataclass(frozen=True)
class NodeSpec:
    """What is known about one node at one interval, before pricing."""

    name: str
    instance_type: Optional[str] = None
    provider: Optional[str] = None
    region: Optional[str] = None
    cpu_capacity_cores: Optional[float] = None
    memory_capacity_bytes: Optional[float] = None
    #: Power profile: idle (min) and full-load (max) watts, vCPUs, provenance.
    idle_watts: float = 0.0
    max_watts: float = 0.0
    vcores: Optional[float] = None
    power_source: str = PowerSource.DEFAULT_PROFILE.value
    #: Whole-node embodied emissions per hour (g CO2e / h); 0 when unknown.
    embodied_g_per_hour: float = 0.0
    gpu_unattributed_watts: float = 0.0
    is_fallback: bool = False


@dataclass
class AllocatedWindow:
    """Allocated pod rows of one node and interval, and its idle row (if any)."""

    rows: List[CombinedMetric] = field(default_factory=list)
    idle: Optional[NodeIdleMetric] = None


def _is_observed_cost(row: CombinedMetric) -> bool:
    source = (row.cost_source or "").strip().lower()
    if source in ALLOCATED_COST_SOURCES:
        return True
    # Legacy rows without provenance: a positive cost came from OpenCost.
    return not source and (row.total_cost or 0.0) > 0


def embodied_g_per_hour(profile: Optional[Mapping[str, Any]]) -> float:
    """Whole-node embodied emissions per hour from a Boavizta cache entry."""
    if not profile:
        return 0.0
    gwp = profile.get("gwp_manufacture")
    lifespan = profile.get("lifespan_hours")
    try:
        gwp_kg = float(gwp) if gwp is not None else 0.0
        hours = float(lifespan) if lifespan is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    return gwp_kg * 1000.0 / hours if gwp_kg > 0 and hours > 0 else 0.0


class MetricRowAllocator:
    """Applies :meth:`ImpactModel.allocate` to the stored rows of one node and interval."""

    def __init__(
        self,
        config: Any,
        *,
        model: Optional[ImpactModel] = None,
        catalog: Optional[PricingCatalog] = None,
    ):
        self.config = config
        self.model: ImpactModel = model if model is not None else DEFAULT_IMPACT_MODEL
        self.catalog = catalog if catalog is not None else PricingCatalog()
        self.custom_rates = CustomRates.from_config(config)
        self.memory_watts_per_gib = memory_watts_per_gib(config)
        cluster = getattr(config, "CLUSTER_NAME", "")
        self.cluster_uid = cluster if isinstance(cluster, str) else ""

    def node_profile(
        self,
        spec: NodeSpec,
        rows: Sequence[CombinedMetric],
        *,
        pue: float,
        intensity: float,
        hours: float,
    ) -> NodeImpactProfile:
        """Builds the node profile of one interval: power, embodied, prices, environment.

        Unit prices follow the cost chain (observed per-resource costs of the
        interval's rows, then the catalog, then custom rates). Only rows whose
        cost is really observed (OpenCost/Kubecost) feed the observed tier: a
        stored row with a modeled cost must never be read back as an observed
        price, or a recompute would blend model outputs into "observed" rates.
        """
        observed_rows = [row for row in rows if _is_observed_cost(row)]
        resolver = UnitPriceResolver(
            catalog=self.catalog,
            custom_rates=self.custom_rates,
            observed=observed_rates_from_rows(observed_rows, hours),
        )
        cores = spec.cpu_capacity_cores or spec.vcores or 1.0
        memory_gib = (spec.memory_capacity_bytes or 0.0) / BYTES_PER_GIB
        prices = resolver.resolve(
            node_name=spec.name or None,
            provider=spec.provider,
            region=spec.region,
            instance_type=spec.instance_type,
            vcpus=cores,
            memory_gib=memory_gib or None,
        )
        try:
            power_source = PowerSource(spec.power_source)
        except ValueError:
            power_source = PowerSource.DEFAULT_PROFILE
        return NodeImpactProfile(
            name=spec.name,
            cpu_capacity_cores=float(cores),
            mem_capacity_gib=memory_gib,
            idle_watts=max(spec.idle_watts, 0.0),
            max_watts=max(spec.max_watts, 0.0),
            embodied_g_per_hour=max(spec.embodied_g_per_hour, 0.0),
            prices=prices,
            pue=pue,
            grid_intensity_g_per_kwh=intensity,
            power_source=power_source,
            instance_type=spec.instance_type,
            region=spec.region,
            provider=spec.provider,
            is_fallback=spec.is_fallback,
            memory_watts_per_gib=self.memory_watts_per_gib,
            gpu_unattributed_watts=max(spec.gpu_unattributed_watts, 0.0),
        )

    def modeled_prices(self, node: NodeImpactProfile) -> UnitPrices:
        """Unit prices of the node from list prices or custom rates, never from observed costs.

        A row without an observed cost is priced at these: a cost derived from
        other pods' OpenCost costs is still a model output and must not carry
        the ``opencost`` label that measurement trusts.
        """
        if node.prices.source not in ALLOCATED_COST_SOURCES:
            return node.prices
        resolver = UnitPriceResolver(catalog=self.catalog, custom_rates=self.custom_rates)
        return resolver.resolve(
            node_name=None,
            provider=node.provider,
            region=node.region,
            instance_type=node.instance_type,
            vcpus=node.cpu_capacity_cores,
            memory_gib=node.mem_capacity_gib or None,
        )

    def _ref(self, row: CombinedMetric) -> WorkloadRef:
        return WorkloadRef(cluster_uid=self.cluster_uid, namespace=row.namespace, kind="Pod", name=row.pod_name)

    def allocate(
        self,
        rows: Sequence[CombinedMetric],
        node: NodeImpactProfile,
        hours: float,
        *,
        emit_idle: bool = True,
        idle_fields: Optional[Mapping[str, Any]] = None,
    ) -> AllocatedWindow:
        """Allocates the pod rows of one node interval; returns updated copies and the idle row.

        ``rows`` must be pod rows (no container rows) of the same node and
        interval, at most one per pod. ``emit_idle`` is False for rows whose
        node is unknown: their synthetic node has no idle capacity to report.
        """
        if not rows:
            return AllocatedWindow()
        if not node.name:
            node = replace(node, name=UNRESOLVED_NODE)
        refs: Dict[WorkloadRef, CombinedMetric] = {}
        samples = []
        for row in rows:
            ref = self._ref(row)
            refs[ref] = row
            # Rows without a resolved node (``node=None``) are allocated to the
            # synthetic node they were grouped under, never left unallocated.
            samples.append(replace(sample_from_metric(row, ref, hours), node=node.name))
        result = self.model.allocate(ClusterSnapshot(self.cluster_uid, hours, {node.name: node}, samples))
        by_ref: Dict[WorkloadRef, WorkloadImpact] = {}
        idle_impact: Optional[WorkloadImpact] = None
        for impact in result.impacts:
            if impact.scope == IDLE_SCOPE:
                idle_impact = impact
            else:
                by_ref[impact.ref] = impact

        modeled = self.modeled_prices(node)
        allocated: List[CombinedMetric] = []
        for ref, row in refs.items():
            impact = by_ref.get(ref)
            if impact is None:  # pragma: no cover - every sample of a known node is allocated
                allocated.append(row)
                continue
            allocated.append(self._apply(row, impact, node, modeled))

        idle: Optional[NodeIdleMetric] = None
        if emit_idle and idle_impact is not None:
            idle = self._idle_row(idle_impact, node, modeled, rows, samples, hours, idle_fields or {})
        return AllocatedWindow(rows=allocated, idle=idle)

    def _apply(
        self, row: CombinedMetric, impact: WorkloadImpact, node: NodeImpactProfile, modeled: UnitPrices
    ) -> CombinedMetric:
        update: Dict[str, Any] = {
            "joules": impact.energy_kwh.total * JOULES_PER_KWH,
            "co2e_grams": impact.co2e_operational_g,
            "embodied_co2e_grams": impact.co2e_embodied_g,
            "model_version": impact.model_version,
        }
        if not _is_observed_cost(row):
            # Allocation cost is share × capacity × unit price, so re-pricing a
            # share at the modeled prices is a per-resource rescale.
            cpu = _rescale(impact.cost_allocated.cpu, node.prices.cpu_core_hour, modeled.cpu_core_hour)
            ram = _rescale(impact.cost_allocated.ram, node.prices.ram_gib_hour, modeled.ram_gib_hour)
            update.update(
                total_cost=cpu + ram,
                cost_cpu=cpu,
                cost_ram=ram,
                cost_storage=None,
                cost_network=None,
                cost_lb=None,
                cost_source=modeled.source.value,
            )
            reason = (
                f"{MODELED_COST_REASON_PREFIX} '{row.pod_name}'; cost allocated by reservation at "
                f"{modeled.source.value} unit prices ({modeled.detail or 'unspecified'})"
            )
            reasons = [r for r in row.estimation_reasons if not r.startswith("No cost data for pod")]
            reasons = [r for r in reasons if not r.startswith(MODELED_COST_REASON_PREFIX)]
            update["estimation_reasons"] = [*reasons, reason]
            update["is_estimated"] = True
        return row.model_copy(update=update)

    def _idle_row(
        self,
        impact: WorkloadImpact,
        node: NodeImpactProfile,
        modeled: UnitPrices,
        rows: Sequence[CombinedMetric],
        samples: Sequence[Any],
        hours: float,
        fields: Mapping[str, Any],
    ) -> NodeIdleMetric:
        """Builds the ``__idle__`` row of a node interval.

        OpenCost idle cost is not queried, so the unreserved capacity is always
        priced at the modeled (catalog or custom) prices and labeled with that
        tier, never with the observed tier of the pods' OpenCost costs.
        """
        reserved_cpu = sum(sample.cpu_reserved_cores * sample.hours for sample in samples)
        reserved_mem = sum(sample.mem_reserved_gib * sample.hours for sample in samples)
        unreserved_cpu = max(node.allocatable_cpu * hours - reserved_cpu, 0.0) / hours if hours > 0 else 0.0
        unreserved_mem = max(node.allocatable_mem * hours - reserved_mem, 0.0) / hours if hours > 0 else 0.0
        cost_cpu = _rescale(impact.cost_allocated.cpu, node.prices.cpu_core_hour, modeled.cpu_core_hour)
        cost_ram = _rescale(impact.cost_allocated.ram, node.prices.ram_gib_hour, modeled.ram_gib_hour)
        first = rows[0]
        timestamp = fields.get("timestamp") or first.timestamp
        if timestamp is None:
            raise ValueError("idle rows require a timestamp")
        return NodeIdleMetric(
            node=node.name,
            timestamp=timestamp,
            duration_seconds=int(round(hours * 3600)),
            joules=impact.energy_kwh.total * JOULES_PER_KWH,
            co2e_grams=impact.co2e_operational_g,
            embodied_co2e_grams=impact.co2e_embodied_g,
            total_cost=cost_cpu + cost_ram,
            cost_cpu=cost_cpu,
            cost_ram=cost_ram,
            cost_source=modeled.source.value,
            cpu_unreserved_cores=unreserved_cpu,
            memory_unreserved_bytes=unreserved_mem * BYTES_PER_GIB,
            gpu_power_watts=node.gpu_unattributed_watts or None,
            pue=node.pue,
            grid_intensity=node.grid_intensity_g_per_kwh,
            emaps_zone=fields.get("emaps_zone", first.emaps_zone),
            power_source=node.power_source.value,
            intensity_source=fields.get("intensity_source", first.intensity_source),
            model_version=impact.model_version,
        )


def _rescale(cost: float, price: float, new_price: float) -> float:
    return cost * new_price / price if price > 0 else 0.0


def group_rows_by_node_interval(
    rows: Sequence[CombinedMetric],
) -> Dict[Tuple[str, Optional[datetime]], List[CombinedMetric]]:
    """Groups pod rows (container rows excluded) by ``(node, timestamp)``."""
    groups: Dict[Tuple[str, Optional[datetime]], List[CombinedMetric]] = {}
    for row in rows:
        if row.container_name:
            continue
        groups.setdefault((row.node or "", row.timestamp), []).append(row)
    return groups


def is_modeled_cost(source: Optional[str]) -> bool:
    """True for cost tiers the model computes itself (catalog or custom rates)."""
    return (source or "") in {CostSource.PRICING_CATALOG.value, CostSource.CUSTOM_RATE.value}


def container_gpu_fields(pod: CombinedMetric, container_count: int) -> Dict[str, Any]:
    """GPU fields of a pod's container rows.

    DCGM attributes GPU power and utilization to pods, not containers: a
    single container inherits them, several containers get none, so summing
    container rows never counts the pod's GPU more than once.
    """
    if container_count == 1:
        return {"gpu_power_watts": pod.gpu_power_watts, "gpu_usage_millicores": pod.gpu_usage_millicores}
    return {"gpu_power_watts": None, "gpu_usage_millicores": None}
