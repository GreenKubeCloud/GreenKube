# src/greenkube/impact/model.py
"""The Impact Model: one versioned model for allocation and projection.

``ReservationAwareImpactModel`` is the v1 implementation. Its formulas are
specified in ``docs/methodology/impact-model.md``; keep both in sync and bump
:data:`~greenkube.impact.types.IMPACT_MODEL_VERSION` when numbers change.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

from greenkube.impact.allocation import allocate_snapshot
from greenkube.impact.types import (
    HOURS_PER_YEAR,
    IMPACT_MODEL_VERSION,
    AllocationResult,
    AssetDeletion,
    CapacityDelta,
    ChangeKind,
    ClusterSnapshot,
    Co2eBreakdown,
    ContainerResourceChange,
    CostBreakdown,
    CostSource,
    ImpactDelta,
    NodeImpactProfile,
    NodePoolChange,
    NodePoolContext,
    ProposedChange,
    ReplicaScheduleChange,
    ResourceSample,
    WorkloadBaseline,
    WorkloadDeletion,
    carbon_value,
    parse_cost_source,
)

#: Confidence in a unit price, by cost tier (documented in the methodology).
PRICE_CONFIDENCE = {
    CostSource.OPENCOST: 0.9,
    CostSource.KUBECOST: 0.9,
    CostSource.PRICING_CATALOG: 0.75,
    CostSource.CUSTOM_RATE: 0.7,
    CostSource.NONE: 0.0,
}

#: Confidence of the OpenCost default rates (a generic price, not this cluster's).
DEFAULT_RATE_CONFIDENCE = 0.5

#: Ceiling on the confidence of prices apportioned from an observed total: the
#: CPU/RAM split is modeled, and the total may include storage or network cost.
APPORTIONED_CONFIDENCE = 0.8

#: ``detail`` of observed prices taken from a sample's CPU and RAM cost split.
OBSERVED_COMPONENTS = "observed_components"

#: ``detail`` of observed prices apportioned from a sample's total cost.
OBSERVED_TOTAL_APPORTIONED = "observed_total_apportioned"

#: Unit prices listed in ``assumptions`` are capped to keep payloads small.
MAX_LISTED_PRICES = 10


class ImpactModel(Protocol):
    """Contract shared by allocation (measurement) and projection (estimation)."""

    version: str

    def allocate(self, snapshot: ClusterSnapshot) -> AllocationResult:
        """Per-workload allocation of one window, plus an ``__idle__`` entry per node."""
        ...

    def project(self, baseline: WorkloadBaseline, change: ProposedChange, pool: NodePoolContext) -> ImpactDelta:
        """Expected annual effect of a proposed change."""
        ...


def price_confidence(node: NodeImpactProfile) -> float:
    """Confidence in a node's unit prices, from the tier that produced them."""
    if node.prices.source == CostSource.CUSTOM_RATE and node.prices.detail != "configured":
        return DEFAULT_RATE_CONFIDENCE
    return PRICE_CONFIDENCE.get(node.prices.source, DEFAULT_RATE_CONFIDENCE)


@dataclass(frozen=True)
class SamplePrices:
    """Price of one reserved core-hour and one reserved GiB-hour for one sample.

    ``observed`` prices come from the sample's own allocated cost (the observed
    tier of the cost chain); otherwise they are the node's unit prices.
    """

    cpu_reserved_core_hour: float
    ram_reserved_gib_hour: float
    source: CostSource
    detail: str
    currency: str
    confidence: float
    observed: bool
    #: ``cost_source`` label that is not a known :class:`CostSource` (kept for provenance).
    raw_source: Optional[str] = None


def observed_cost_breakdown(sample: ResourceSample) -> Optional[CostBreakdown]:
    """Observed allocated cost of a sample, split by component, or None when unknown.

    Components the source did not split go to ``other``. A split larger than the
    total is scaled down to the total, which is trusted.
    """
    total = sample.observed_cost_total
    if total is None or total <= 0:
        return None
    components = {
        "cpu": max(sample.observed_cost_cpu or 0.0, 0.0),
        "ram": max(sample.observed_cost_ram or 0.0, 0.0),
        "storage": max(sample.observed_cost_storage or 0.0, 0.0),
        "network": max(sample.observed_cost_network or 0.0, 0.0),
        "lb": max(sample.observed_cost_lb or 0.0, 0.0),
    }
    split = sum(components.values())
    if split > total:
        components = {name: value * total / split for name, value in components.items()}
        split = total
    return CostBreakdown(other=max(total - split, 0.0), **components)


def sample_prices(sample: ResourceSample, node: NodeImpactProfile, currency: str) -> SamplePrices:
    """Prices of a sample's CPU and RAM reservation, anchored to its observed cost.

    1. The sample has an observed CPU and RAM cost: each price is that cost
       divided by the sample's reserved core-hours (GiB-hours).
    2. The sample has only an observed total (OpenCost ``totalCost`` today):
       the unsplit total is apportioned to the resources without an observed
       cost, weighted by their modeled cost at the node's unit prices.
    3. No observed cost: the node's unit prices (catalog or custom rates).

    Under 1 and 2 the CPU and RAM cost of a sample never exceeds its observed
    total, so request cuts can never claim more than the container is charged
    (BUG-15). ``currency`` labels observed prices (the configured currency).
    """
    hours = max(sample.hours, 0.0)
    cpu_quantity = sample.cpu_reserved_cores * hours
    mem_quantity = sample.mem_reserved_gib * hours
    node_cpu = node.cpu_price_per_reserved_core_hour
    node_ram = node.ram_price_per_reserved_gib_hour
    observed = observed_cost_breakdown(sample)
    if observed is None:
        return SamplePrices(
            cpu_reserved_core_hour=node_cpu,
            ram_reserved_gib_hour=node_ram,
            source=node.prices.source,
            detail=node.prices.detail,
            currency=node.prices.currency,
            confidence=price_confidence(node),
            observed=False,
        )

    cpu_known = sample.observed_cost_cpu is not None
    ram_known = sample.observed_cost_ram is not None
    cpu_cost = observed.cpu if cpu_known else 0.0
    ram_cost = observed.ram if ram_known else 0.0
    source, raw_source = parse_cost_source(sample.observed_cost_source)
    confidence = PRICE_CONFIDENCE.get(source, DEFAULT_RATE_CONFIDENCE)
    detail = OBSERVED_COMPONENTS
    if not (cpu_known and ram_known):
        # OpenCost apportioning: split what the source left unsplit by the
        # modeled cost of each resource at the node's unit prices.
        cpu_weight = 0.0 if cpu_known else cpu_quantity * node_cpu
        ram_weight = 0.0 if ram_known else mem_quantity * node_ram
        weight = cpu_weight + ram_weight
        if weight > 0:
            cpu_cost += observed.other * cpu_weight / weight
            ram_cost += observed.other * ram_weight / weight
        detail = OBSERVED_TOTAL_APPORTIONED
        confidence = min(confidence, APPORTIONED_CONFIDENCE)
    return SamplePrices(
        cpu_reserved_core_hour=cpu_cost / cpu_quantity if cpu_quantity > 0 else 0.0,
        ram_reserved_gib_hour=ram_cost / mem_quantity if mem_quantity > 0 else 0.0,
        source=source,
        detail=detail,
        currency=currency,
        confidence=confidence,
        observed=True,
        raw_source=raw_source,
    )


@dataclass
class _PriceTally:
    """Observed prices of one tier, aggregated over the samples they priced."""

    cpu_cost: float = 0.0
    cpu_core_hours: float = 0.0
    ram_cost: float = 0.0
    ram_gib_hours: float = 0.0


@dataclass
class _Accumulator:
    """Window totals of a projection before annualization."""

    cost: CostBreakdown = field(default_factory=CostBreakdown)
    co2e: Co2eBreakdown = field(default_factory=Co2eBreakdown)
    cpu_core_hours: float = 0.0
    mem_gib_hours: float = 0.0
    replica_hours: float = 0.0
    weight_hours: float = 0.0
    confidence_weighted: float = 0.0
    pue_weighted: float = 0.0
    intensity_weighted: float = 0.0
    nodes: Dict[str, NodeImpactProfile] = field(default_factory=dict)
    #: Nodes whose unit prices priced part of the projection.
    priced_nodes: Dict[str, NodeImpactProfile] = field(default_factory=dict)
    #: Observed prices, keyed by (source, detail, currency).
    observed_prices: Dict[Tuple[str, str, str], _PriceTally] = field(default_factory=dict)
    currencies: set = field(default_factory=set)
    observed_cost_sources: Counter = field(default_factory=Counter)

    def track(
        self,
        node: NodeImpactProfile,
        sample: ResourceSample,
        pue: float,
        intensity: float,
        prices: SamplePrices,
    ) -> None:
        """Records a sample's environment and the prices that priced it."""
        self.nodes.setdefault(node.name, node)
        weight = max(sample.hours, 0.0)
        self.weight_hours += weight
        self.confidence_weighted += prices.confidence * weight
        self.pue_weighted += pue * weight
        self.intensity_weighted += intensity * weight
        self.currencies.add(prices.currency)
        if not prices.observed:
            self.priced_nodes.setdefault(node.name, node)
            return
        self.observed_cost_sources[prices.raw_source or prices.source.value] += 1
        tally = self.observed_prices.setdefault((prices.source.value, prices.detail, prices.currency), _PriceTally())
        cpu_quantity = sample.cpu_reserved_cores * weight
        mem_quantity = sample.mem_reserved_gib * weight
        tally.cpu_cost += prices.cpu_reserved_core_hour * cpu_quantity
        tally.cpu_core_hours += cpu_quantity
        tally.ram_cost += prices.ram_reserved_gib_hour * mem_quantity
        tally.ram_gib_hours += mem_quantity

    def track_node(self, node: NodeImpactProfile, hours: float) -> None:
        """Records a whole node priced at its unit prices (node-pool changes)."""
        self.nodes.setdefault(node.name, node)
        self.priced_nodes.setdefault(node.name, node)
        self.currencies.add(node.prices.currency)
        self.weight_hours += hours
        self.confidence_weighted += price_confidence(node) * hours
        self.pue_weighted += node.pue * hours
        self.intensity_weighted += node.grid_intensity_g_per_kwh * hours

    def listed_prices(self) -> List[Dict[str, Any]]:
        """Distinct unit prices used by a projection, for the PR body and the API."""
        listed: List[Dict[str, Any]] = []
        for (source, detail, currency), tally in sorted(self.observed_prices.items()):
            listed.append(
                {
                    "cpu_core_hour": tally.cpu_cost / tally.cpu_core_hours if tally.cpu_core_hours > 0 else 0.0,
                    "ram_gib_hour": tally.ram_cost / tally.ram_gib_hours if tally.ram_gib_hours > 0 else 0.0,
                    "currency": currency,
                    "source": source,
                    "detail": detail,
                }
            )
        return (listed + _listed_prices(self.priced_nodes))[:MAX_LISTED_PRICES]

    def add_cost(self, cost: CostBreakdown) -> None:
        self.cost = self.cost + cost

    def add_co2e(
        self,
        idle: float = 0.0,
        dynamic: float = 0.0,
        embodied: float = 0.0,
        memory: float = 0.0,
        gpu: float = 0.0,
    ) -> None:
        self.co2e = Co2eBreakdown(
            idle_operational=self.co2e.idle_operational + idle,
            dynamic_operational=self.co2e.dynamic_operational + dynamic,
            embodied=self.co2e.embodied + embodied,
            memory_operational=self.co2e.memory_operational + memory,
            gpu_operational=self.co2e.gpu_operational + gpu,
        )


class ReservationAwareImpactModel:
    """Impact Model v1: reservation-aware allocation and projection."""

    version = IMPACT_MODEL_VERSION

    def allocate(self, snapshot: ClusterSnapshot) -> AllocationResult:
        return allocate_snapshot(snapshot)

    def project(self, baseline: WorkloadBaseline, change: ProposedChange, pool: NodePoolContext) -> ImpactDelta:
        if isinstance(change, ContainerResourceChange):
            return self._project_container_resources(baseline, change, pool)
        if isinstance(change, WorkloadDeletion):
            return self._project_sample_removal(
                baseline, baseline.samples, ChangeKind.WORKLOAD_DELETION, pool, 1.0, "deletion"
            )
        if isinstance(change, ReplicaScheduleChange):
            removed = [
                sample
                for sample in baseline.samples
                if sample.timestamp is not None and _utc_hour(sample.timestamp) in change.removed_hours_of_day
            ]
            factor, source, reasons, pools = pool.realization_for(removed or baseline.samples)
            delta = self._project_sample_removal(baseline, removed, ChangeKind.REPLICA_SCHEDULE, pool, factor, source)
            _record_realization(delta, reasons, pools)
            delta.assumptions["removed_hours_of_day"] = sorted(change.removed_hours_of_day)
            return delta
        if isinstance(change, AssetDeletion):
            return self._project_asset_deletion(change, pool)
        if isinstance(change, NodePoolChange):
            return self._project_node_pool(baseline, change, pool)
        raise TypeError(f"Unsupported change type: {type(change).__name__}")

    # ------------------------------------------------------------------ #
    # Container requests                                                  #
    # ------------------------------------------------------------------ #

    def _project_container_resources(
        self,
        baseline: WorkloadBaseline,
        change: ContainerResourceChange,
        pool: NodePoolContext,
    ) -> ImpactDelta:
        acc = _Accumulator()
        for sample in baseline.samples:
            node = pool.node_for(sample.node)
            pue, intensity = _sample_environment(sample, node)
            prices = sample_prices(sample, node, pool.currency)
            acc.track(node, sample, pue, intensity, prices)
            hours = max(sample.hours, 0.0)
            acc.replica_hours += hours

            cpu_released = 0.0
            cpu_share_released = 0.0
            if change.cpu_request_cores is not None:
                current, proposed = change.cpu_request_cores
                cpu_released = _released(current, proposed, sample.cpu_usage_cores)
                cpu_share_released = _released(current, proposed, sample.cpu_usage_cores, node.allocatable_cpu)
            mem_released = 0.0
            mem_share_released = 0.0
            if change.mem_request_gib is not None:
                current, proposed = change.mem_request_gib
                mem_released = _released(current, proposed, sample.mem_usage_gib)
                mem_share_released = _released(current, proposed, sample.mem_usage_gib, node.allocatable_mem)

            acc.cpu_core_hours += cpu_released * hours
            acc.mem_gib_hours += mem_released * hours
            acc.add_cost(
                CostBreakdown(
                    cpu=cpu_released * hours * prices.cpu_reserved_core_hour,
                    ram=mem_released * hours * prices.ram_reserved_gib_hour,
                )
            )
            # Request cuts do not change usage, so dynamic power is unchanged:
            # only the reservation-allocated idle power, memory power and
            # embodied share move. A reservation never holds more than the
            # whole node's idle power or memory power.
            idle_kwh = cpu_share_released * hours * node.idle_watts_per_reserved_core / 1000.0
            memory_kwh = mem_share_released * hours * max(node.memory_watts_per_gib, 0.0) / 1000.0
            acc.add_co2e(
                idle=idle_kwh * pue * intensity,
                memory=memory_kwh * pue * intensity,
                embodied=cpu_share_released * hours * node.embodied_g_per_reserved_core_hour,
            )

        factor, source, reasons, pools = pool.realization_for(baseline.samples)
        delta = self._finalize(
            ChangeKind.CONTAINER_RESOURCES,
            acc,
            baseline.window_hours,
            pool,
            realization_factor=factor,
            realization_source=source,
            co2e_basis="idle_and_embodied_share_released",
        )
        _record_realization(delta, reasons, pools)
        change_details: Dict[str, Any] = {}
        if change.cpu_request_cores is not None:
            change_details["cpu_request_cores"] = list(change.cpu_request_cores)
        if change.mem_request_gib is not None:
            change_details["mem_request_gib"] = list(change.mem_request_gib)
        delta.assumptions["change"] = change_details
        return delta

    # ------------------------------------------------------------------ #
    # Deletion and schedule changes                                       #
    # ------------------------------------------------------------------ #

    def _project_sample_removal(
        self,
        baseline: WorkloadBaseline,
        samples,
        kind: ChangeKind,
        pool: NodePoolContext,
        realization_factor: float,
        realization_source: str,
    ) -> ImpactDelta:
        acc = _Accumulator()
        for sample in samples:
            node = pool.node_for(sample.node)
            pue, intensity = _sample_environment(sample, node)
            prices = sample_prices(sample, node, pool.currency)
            acc.track(node, sample, pue, intensity, prices)
            hours = max(sample.hours, 0.0)
            acc.replica_hours += hours

            cpu_reserved = sample.cpu_reserved_cores
            mem_reserved = sample.mem_reserved_gib
            acc.cpu_core_hours += cpu_reserved * hours
            acc.mem_gib_hours += mem_reserved * hours
            acc.add_cost(_sample_allocated_cost(sample, prices))

            # Shares are capped at the node: one workload never holds more than
            # the whole node's idle power, dynamic power or embodied emissions.
            cpu_share = _capped(cpu_reserved, node.allocatable_cpu)
            used = _capped(max(sample.cpu_usage_cores or 0.0, 0.0), node.cpu_capacity_cores)
            idle_kwh = cpu_share * hours * node.idle_watts_per_reserved_core / 1000.0
            dynamic_kwh = used * hours * node.dynamic_watts_per_used_core / 1000.0
            mem_share = _capped(mem_reserved, node.allocatable_mem)
            memory_kwh = mem_share * hours * max(node.memory_watts_per_gib, 0.0) / 1000.0
            gpu_kwh = max(sample.gpu_watts or 0.0, 0.0) * hours / 1000.0
            acc.add_co2e(
                idle=idle_kwh * pue * intensity,
                dynamic=dynamic_kwh * pue * intensity,
                memory=memory_kwh * pue * intensity,
                gpu=gpu_kwh * pue * intensity,
                embodied=cpu_share * hours * node.embodied_g_per_reserved_core_hour,
            )

        return self._finalize(
            kind,
            acc,
            baseline.window_hours,
            pool,
            realization_factor=realization_factor,
            realization_source=realization_source,
            co2e_basis="allocated_share_released",
        )

    def _project_asset_deletion(self, change: AssetDeletion, pool: NodePoolContext) -> ImpactDelta:
        cost_value = max(float(change.allocated_cost_annual), 0.0)
        component = change.component if change.component in ("storage", "network", "lb") else "other"
        cost = CostBreakdown(**{component: cost_value})
        if change.cost_source == CostSource.CUSTOM_RATE:
            confidence = DEFAULT_RATE_CONFIDENCE
        else:
            confidence = PRICE_CONFIDENCE.get(change.cost_source, DEFAULT_RATE_CONFIDENCE)
        return ImpactDelta(
            kind=ChangeKind.ASSET_DELETION,
            allocated_cost_annual=cost.total,
            cost_breakdown_annual=cost,
            capacity_released=CapacityDelta(),
            expected_billable_cost_annual=cost.total,
            realization_factor=1.0,
            co2e_allocated_annual_g=0.0,
            co2e_expected_annual_g=0.0,
            co2e_breakdown_annual_g=Co2eBreakdown(),
            co2e_modeled=False,
            carbon_price_per_tonne=pool.carbon_price_per_tonne,
            value_annual=carbon_value(cost.total, 0.0, pool.carbon_price_per_tonne),
            currency=pool.currency,
            confidence=confidence,
            assumptions={
                "allocation_policy": "reservation_aware",
                "asset_kind": change.asset_kind,
                "cost_source": change.cost_source.value,
                "cost_detail": change.detail,
                "realization_factor_source": "deletion",
                "co2e_basis": "not_modeled",
                "notes": ["The energy model does not cover storage, network or load-balancer energy."],
            },
        )

    # ------------------------------------------------------------------ #
    # Node pools                                                          #
    # ------------------------------------------------------------------ #

    def _project_node_pool(
        self, baseline: WorkloadBaseline, change: NodePoolChange, pool: NodePoolContext
    ) -> ImpactDelta:
        acc = _Accumulator()
        node_hours = 0.0
        missing: List[str] = []
        for name in change.removed_nodes:
            if name not in pool.nodes:
                missing.append(name)
                continue
            node = pool.nodes[name]
            hours = float(HOURS_PER_YEAR)
            node_hours += hours
            acc.track_node(node, hours)
            acc.cpu_core_hours += node.cpu_capacity_cores * hours
            acc.mem_gib_hours += node.mem_capacity_gib * hours
            acc.add_cost(
                CostBreakdown(
                    cpu=node.cpu_capacity_cores * node.prices.cpu_core_hour * hours,
                    ram=node.mem_capacity_gib * node.prices.ram_gib_hour * hours,
                )
            )
            # Workloads move to the remaining nodes, so dynamic power stays;
            # the removed node's idle power, memory power, idle GPUs and
            # embodied emissions go away.
            intensity = node.pue * node.grid_intensity_g_per_kwh
            acc.add_co2e(
                idle=node.idle_watts * hours / 1000.0 * intensity,
                memory=node.memory_watts * hours / 1000.0 * intensity,
                gpu=max(node.gpu_unattributed_watts, 0.0) * hours / 1000.0 * intensity,
                embodied=node.embodied_g_per_hour * hours,
            )

        reasons: Tuple[str, ...] = ()
        pools: Tuple = ()
        if change.action_is_consolidation:
            realization_factor, realization_source = 1.0, "consolidation_action"
        else:
            node_samples = [ResourceSample(ref=baseline.ref, hours=1.0, node=name) for name in change.removed_nodes]
            realization_factor, realization_source, reasons, pools = pool.realization_for(node_samples)
        delta = self._finalize(
            ChangeKind.NODE_POOL,
            acc,
            float(HOURS_PER_YEAR),
            pool,
            realization_factor=realization_factor,
            realization_source=realization_source,
            co2e_basis="node_idle_and_embodied_removed",
            node_hours=node_hours,
        )
        _record_realization(delta, reasons, pools)
        delta.assumptions["removed_nodes"] = list(change.removed_nodes)
        if missing:
            delta.assumptions["unknown_nodes"] = missing
        return delta

    # ------------------------------------------------------------------ #
    # Shared                                                              #
    # ------------------------------------------------------------------ #

    def _finalize(
        self,
        kind: ChangeKind,
        acc: _Accumulator,
        window_hours: float,
        pool: NodePoolContext,
        *,
        realization_factor: float,
        co2e_basis: str,
        realization_source: Optional[str] = None,
        node_hours: float = 0.0,
    ) -> ImpactDelta:
        factor = HOURS_PER_YEAR / window_hours if window_hours > 0 else 0.0
        realization = min(max(float(realization_factor), 0.0), 1.0)
        cost = acc.cost.scaled(factor)
        co2e = acc.co2e.scaled(factor)
        expected_cost = cost.total * realization
        expected_co2e = co2e.total * realization
        confidence = acc.confidence_weighted / acc.weight_hours if acc.weight_hours > 0 else 0.0
        currencies = sorted(acc.currencies) or [pool.currency]
        currency = currencies[0] if len(currencies) == 1 else "mixed"

        assumptions: Dict[str, Any] = {
            "allocation_policy": "reservation_aware",
            "window_hours": window_hours,
            "annualization_factor": factor,
            "unit_prices": acc.listed_prices(),
            "realization_factor_source": realization_source or pool.realization_source,
            "co2e_basis": co2e_basis,
        }
        if acc.weight_hours > 0:
            assumptions["pue"] = acc.pue_weighted / acc.weight_hours
            assumptions["grid_intensity_g_per_kwh"] = acc.intensity_weighted / acc.weight_hours
        power_sources = sorted({node.power_source.value for node in acc.nodes.values()})
        if power_sources:
            assumptions["power_sources"] = power_sources
        if any(node.is_fallback for node in acc.nodes.values()):
            assumptions["unknown_node_profile"] = True
        if acc.observed_cost_sources:
            assumptions["observed_cost_sources"] = dict(acc.observed_cost_sources)
        if currency == "mixed":
            assumptions["currencies"] = currencies

        return ImpactDelta(
            kind=kind,
            allocated_cost_annual=cost.total,
            cost_breakdown_annual=cost,
            capacity_released=CapacityDelta(
                cpu_core_hours=acc.cpu_core_hours * factor,
                mem_gib_hours=acc.mem_gib_hours * factor,
                replica_hours=acc.replica_hours * factor,
                node_hours=node_hours,
            ),
            expected_billable_cost_annual=expected_cost,
            realization_factor=realization,
            co2e_allocated_annual_g=co2e.total,
            co2e_expected_annual_g=expected_co2e,
            co2e_breakdown_annual_g=co2e,
            co2e_modeled=True,
            carbon_price_per_tonne=pool.carbon_price_per_tonne,
            value_annual=carbon_value(expected_cost, expected_co2e, pool.carbon_price_per_tonne),
            currency=currency,
            confidence=round(confidence, 4),
            assumptions=assumptions,
        )


def _record_realization(delta: ImpactDelta, reasons: Tuple[str, ...], pools: Tuple) -> None:
    """Adds why the realization factor has its value to a projection's assumptions (B6)."""
    if reasons:
        delta.assumptions["realization_reasons"] = list(reasons)
    if pools:
        delta.assumptions["realization_pools"] = list(pools)


def _utc_hour(timestamp: datetime) -> int:
    """Hour of day in UTC; naive timestamps are already UTC in GreenKube storage."""
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.hour


def _capped(value: float, ceiling: float) -> float:
    """``value`` limited to ``ceiling`` when the ceiling is known (positive)."""
    return min(value, ceiling) if ceiling > 0 else value


def _released(current: float, proposed: float, usage: Optional[float], ceiling: float = 0.0) -> float:
    """Reservation released by moving a request, under ``max(request, usage)`` reservation.

    With a positive ``ceiling`` (the node's allocatable capacity) both
    reservations are capped at it first, which bounds the released node share.
    """
    used = max(usage or 0.0, 0.0)
    before = _capped(max(current, used, 0.0), ceiling)
    after = _capped(max(proposed, used, 0.0), ceiling)
    return before - after


def _sample_environment(sample: ResourceSample, node: NodeImpactProfile) -> Tuple[float, float]:
    """PUE and grid intensity of a sample, falling back to the node profile."""
    pue = sample.pue if sample.pue is not None and sample.pue > 0 else node.pue
    intensity = (
        sample.grid_intensity_g_per_kwh
        if sample.grid_intensity_g_per_kwh is not None and sample.grid_intensity_g_per_kwh > 0
        else node.grid_intensity_g_per_kwh
    )
    return pue, intensity


def _sample_allocated_cost(sample: ResourceSample, prices: SamplePrices) -> CostBreakdown:
    """Current allocated cost of a sample: observed when known, otherwise modeled."""
    observed = observed_cost_breakdown(sample) if prices.observed else None
    if observed is not None:
        return observed
    hours = max(sample.hours, 0.0)
    return CostBreakdown(
        cpu=sample.cpu_reserved_cores * hours * prices.cpu_reserved_core_hour,
        ram=sample.mem_reserved_gib * hours * prices.ram_reserved_gib_hour,
    )


def _listed_prices(nodes: Dict[str, NodeImpactProfile]) -> List[Dict[str, Any]]:
    """Distinct unit prices used by a projection, for the PR body and the API."""
    listed: List[Dict[str, Any]] = []
    seen = set()
    for name in sorted(nodes):
        prices = nodes[name].prices
        key = (prices.source, prices.detail, prices.cpu_core_hour, prices.ram_gib_hour, prices.currency)
        if key in seen:
            continue
        seen.add(key)
        listed.append(prices.to_dict())
        if len(listed) >= MAX_LISTED_PRICES:
            break
    return listed


#: Default model instance; stateless, safe to share.
DEFAULT_IMPACT_MODEL = ReservationAwareImpactModel()
