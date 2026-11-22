# src/greenkube/impact/model.py
"""The Impact Model: one versioned model for allocation and projection.

``ReservationAwareImpactModel`` is the v1 implementation. Its formulas are
specified in ``docs/methodology/impact-model.md``; keep both in sync and bump
:data:`~greenkube.impact.types.IMPACT_MODEL_VERSION` when numbers change.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
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
    NodePoolContext,
    ProposedChange,
    ResourceSample,
    WorkloadBaseline,
    carbon_value,
    parse_cost_source,
)

#: Confidence in a unit price, by cost tier (documented in the methodology).
PRICE_CONFIDENCE = {
    CostSource.OPENCOST: 0.9,
    CostSource.OPENCOST_DERIVED: 0.9,
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

#: ``detail`` of observed prices taken from a CPU and RAM cost split that covers
#: only part of the sample's hours; the total of the other hours (re-aggregated
#: from 0.3.x rows, F-03) is apportioned.
OBSERVED_COMPONENTS_PARTLY_APPORTIONED = "observed_components_partly_apportioned"

#: Unit prices listed in ``assumptions`` are capped to keep payloads small.
MAX_LISTED_PRICES = 10

#: Shortest coverage a projection is annualized over: a target observed for a
#: few minutes is annualized as if observed for half an hour (one hour before IMP-04), never extrapolated
#: from a single scrape.
MIN_COVERAGE_HOURS = 0.5

#: Coverage below one week is short: weekly cycles are not seen yet. Short
#: coverage is reported and lowers the projection's confidence.
SHORT_COVERAGE_HOURS = 7 * 24.0

#: Lowest confidence multiplier applied for short coverage.
SHORT_COVERAGE_CONFIDENCE_FLOOR = 0.25


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
    #: Hours of the sample whose observed cost had no CPU/RAM split at all and
    #: was apportioned at the node's price ratio (F-03).
    unsplit_hours: float = 0.0


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
       An aggregate whose split covers only some of its hours (hours
       re-aggregated from 0.3.x rows carry a total only, F-03) prices each
       component over the hours that carry it, and apportions the total of
       the other hours the same way. Hours whose observed total is zero
       count as zero cost.
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
    source, raw_source = parse_cost_source(sample.observed_cost_source)
    confidence = PRICE_CONFIDENCE.get(source, DEFAULT_RATE_CONFIDENCE)
    split_hours = sample.observed_cost_split_hours
    unsplit_hours = 0.0
    if split_hours is not None and (cpu_known or ram_known) and split_hours < hours:
        # F-03: the CPU/RAM split covers only ``split_hours`` (the others were
        # re-aggregated from 0.3.x rows with a total only). Each component is
        # priced over the hours that carry it, and the total of the unsplit
        # hours is apportioned like a sample with a total only.
        split_hours = max(split_hours, 0.0)
        rest = hours - split_hours
        given = sample.observed_cost_unsplit_hours
        unsplit_hours = rest if given is None else min(max(given, 0.0), rest)
        unsplit_total = min(max(sample.observed_cost_unsplit_total or 0.0, 0.0), observed.other)
        cpu_cost, ram_cost, apportioned = _observed_cpu_ram_cost(
            observed.cpu if cpu_known else None,
            observed.ram if ram_known else None,
            observed.other - unsplit_total,
            sample.cpu_reserved_cores * split_hours * node_cpu,
            sample.mem_reserved_gib * split_hours * node_ram,
        )
        unsplit_cpu, unsplit_ram, _ = _observed_cpu_ram_cost(
            None,
            None,
            unsplit_total,
            sample.cpu_reserved_cores * unsplit_hours * node_cpu,
            sample.mem_reserved_gib * unsplit_hours * node_ram,
        )
        # Hours whose observed total is zero cost nothing (OpenCost says so):
        # they add reserved quantity but no cost, which keeps every projection
        # bounded by the observed cost (BUG-15).
        cpu_cost += unsplit_cpu
        ram_cost += unsplit_ram
        if unsplit_total > 0:
            detail = OBSERVED_COMPONENTS_PARTLY_APPORTIONED
        else:
            detail = OBSERVED_TOTAL_APPORTIONED if apportioned else OBSERVED_COMPONENTS
            unsplit_hours = 0.0
        if unsplit_total > 0 or apportioned:
            confidence = min(confidence, APPORTIONED_CONFIDENCE)
    else:
        cpu_cost, ram_cost, apportioned = _observed_cpu_ram_cost(
            observed.cpu if cpu_known else None,
            observed.ram if ram_known else None,
            observed.other,
            cpu_quantity * node_cpu,
            mem_quantity * node_ram,
        )
        detail = OBSERVED_TOTAL_APPORTIONED if apportioned else OBSERVED_COMPONENTS
        if not (cpu_known or ram_known):
            unsplit_hours = hours
        if apportioned:
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
        unsplit_hours=unsplit_hours,
    )


def _observed_cpu_ram_cost(
    cpu: Optional[float],
    ram: Optional[float],
    unsplit: float,
    cpu_weight: float,
    ram_weight: float,
) -> Tuple[float, float, bool]:
    """CPU and RAM cost of observed components, with ``unsplit`` cost apportioned to the unknown ones.

    ``cpu`` or ``ram`` is None when the source did not split it; the unsplit
    cost then goes to the unknown components, weighted by their modeled cost
    at the node's unit prices (``cpu_weight``, ``ram_weight``), as OpenCost
    apportions. Returns ``(cpu_cost, ram_cost, apportioned)``.
    """
    if cpu is not None and ram is not None:
        return cpu, ram, False
    cpu_cost = cpu if cpu is not None else 0.0
    ram_cost = ram if ram is not None else 0.0
    cpu_weight = 0.0 if cpu is not None else cpu_weight
    ram_weight = 0.0 if ram is not None else ram_weight
    weight = cpu_weight + ram_weight
    if weight > 0 and unsplit > 0:
        cpu_cost += unsplit * cpu_weight / weight
        ram_cost += unsplit * ram_weight / weight
    return cpu_cost, ram_cost, True


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
    #: Observed hours, and those whose cost had no CPU/RAM split (F-03).
    observed_hours: float = 0.0
    unsplit_cost_hours: float = 0.0

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
        self.observed_hours += weight
        self.unsplit_cost_hours += min(max(prices.unsplit_hours, 0.0), weight)
        tally = self.observed_prices.setdefault((prices.source.value, prices.detail, prices.currency), _PriceTally())
        cpu_quantity = sample.cpu_reserved_cores * weight
        mem_quantity = sample.mem_reserved_gib * weight
        tally.cpu_cost += prices.cpu_reserved_core_hour * cpu_quantity
        tally.cpu_core_hours += cpu_quantity
        tally.ram_cost += prices.ram_reserved_gib_hour * mem_quantity
        tally.ram_gib_hours += mem_quantity

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


#: Share of the coverage span the collector must have observed before a projection loses confidence (IMP-04).
MIN_COLLECTED_SHARE = 0.9
#: Lowest confidence scale a collection gap applies.
COLLECTION_GAP_CONFIDENCE_FLOOR = 0.5


@dataclass(frozen=True)
class Annualization:
    """Hours a projection's window totals are annualized over, and why."""

    hours: float
    assumptions: Dict[str, Any] = field(default_factory=dict)
    confidence_scale: float = 1.0
    notes: Tuple[str, ...] = ()


def annualization(baseline: WorkloadBaseline) -> Annualization:
    """Annualization of a baseline: over the target's observed coverage when known.

    Window totals (cost, CO2e, capacity) are sums over the samples of the
    target, which exist only since the target was first observed. Dividing
    them by the whole lookback would treat a workload that appeared two hours
    ago as if it had run two hours in fourteen days. The model divides by the
    observed coverage instead (from the first observation to the end of the
    lookback, at least :data:`MIN_COVERAGE_HOURS`, at most the lookback), and
    marks coverage below :data:`SHORT_COVERAGE_HOURS` as short.
    """
    coverage = baseline.coverage
    if coverage is None:
        return Annualization(hours=baseline.window_hours)
    lookback = coverage.lookback_hours if coverage.lookback_hours > 0 else baseline.window_hours
    observed = max(float(coverage.observed_hours), 0.0)
    if lookback > 0:
        observed = min(observed, lookback)
    hours = max(observed, MIN_COVERAGE_HOURS)
    assumptions: Dict[str, Any] = {
        "lookback_hours": lookback,
        "coverage_hours": observed,
        "coverage_basis": coverage.basis,
    }
    if coverage.first_observed is not None:
        assumptions["first_observed_at"] = coverage.first_observed.isoformat()
    notes: List[str] = []
    scale = 1.0
    if coverage.collected_hours is not None and coverage.span_hours is not None:
        # IMP-04: hours without collection stay out of the annualization, and say so.
        gap = max(coverage.span_hours - coverage.collected_hours, 0.0)
        assumptions["coverage_span_hours"] = coverage.span_hours
        assumptions["collected_hours"] = coverage.collected_hours
        assumptions["collection_gap_hours"] = gap
        share = coverage.collected_hours / coverage.span_hours if coverage.span_hours > 0 else 1.0
        if share < MIN_COLLECTED_SHARE:
            scale *= max(share, COLLECTION_GAP_CONFIDENCE_FLOOR)
            notes.append(
                f"The collector observed {coverage.collected_hours:.1f} h of the {coverage.span_hours:.1f} h "
                "since the target was first seen: the projection is annualized over the observed hours only."
            )
    if observed < MIN_COVERAGE_HOURS:
        assumptions["minimum_coverage_applied"] = True
    if observed < SHORT_COVERAGE_HOURS and observed < lookback:
        assumptions["short_coverage"] = True
        scale = max(observed / SHORT_COVERAGE_HOURS, SHORT_COVERAGE_CONFIDENCE_FLOOR)
        notes.append(
            f"Annualized from {observed:.1f} h of observation (the target is younger than the "
            f"{lookback:.0f} h lookback): weekly cycles are not seen yet, so the projection is less certain."
        )
    return Annualization(hours=hours, assumptions=assumptions, confidence_scale=scale, notes=tuple(notes))


class ReservationAwareImpactModel:
    """Impact Model v1: reservation-aware allocation and projection."""

    version = IMPACT_MODEL_VERSION

    def allocate(self, snapshot: ClusterSnapshot) -> AllocationResult:
        return allocate_snapshot(snapshot)

    def project(self, baseline: WorkloadBaseline, change: ProposedChange, pool: NodePoolContext) -> ImpactDelta:
        if isinstance(change, ContainerResourceChange):
            return self._project_container_resources(baseline, change, pool)
        if isinstance(change, AssetDeletion):
            return self._project_asset_deletion(change, pool)
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
            annualization(baseline),
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
            capacity_released=CapacityDelta(
                storage_gib=max(float(change.storage_gib), 0.0), load_balancers=max(int(change.load_balancers), 0)
            ),
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
    # Shared                                                              #
    # ------------------------------------------------------------------ #

    def _finalize(
        self,
        kind: ChangeKind,
        acc: _Accumulator,
        annualized: Annualization,
        pool: NodePoolContext,
        *,
        realization_factor: float,
        co2e_basis: str,
        realization_source: Optional[str] = None,
    ) -> ImpactDelta:
        """Annualizes an accumulated projection and applies the realization factor."""
        window_hours = annualized.hours
        factor = HOURS_PER_YEAR / window_hours if window_hours > 0 else 0.0
        realization = min(max(float(realization_factor), 0.0), 1.0)
        cost = acc.cost.scaled(factor)
        co2e = acc.co2e.scaled(factor)
        expected_cost = cost.total * realization
        expected_co2e = co2e.total * realization
        confidence = acc.confidence_weighted / acc.weight_hours if acc.weight_hours > 0 else 0.0
        confidence *= annualized.confidence_scale
        currencies = sorted(acc.currencies) or [pool.currency]
        currency = currencies[0] if len(currencies) == 1 else "mixed"

        assumptions: Dict[str, Any] = {
            "allocation_policy": "reservation_aware",
            "window_hours": window_hours,
            "annualization_factor": factor,
            "unit_prices": acc.listed_prices(),
            "realization_factor_source": realization_source or pool.realization_source,
            "co2e_basis": co2e_basis,
            **annualized.assumptions,
        }
        if annualized.notes:
            assumptions["notes"] = list(annualized.notes)
        if acc.weight_hours > 0:
            assumptions["pue"] = acc.pue_weighted / acc.weight_hours
            assumptions["grid_intensity_g_per_kwh"] = acc.intensity_weighted / acc.weight_hours
        power_sources = sorted({node.power_source.value for node in acc.nodes.values()})
        if power_sources:
            assumptions["power_sources"] = power_sources
        embodied_sources = sorted({node.embodied_source for node in acc.nodes.values() if node.embodied_source})
        if embodied_sources:
            assumptions["embodied_sources"] = embodied_sources
        if any(node.is_fallback for node in acc.nodes.values()):
            assumptions["unknown_node_profile"] = True
        if acc.observed_cost_sources:
            assumptions["observed_cost_sources"] = dict(acc.observed_cost_sources)
        if acc.unsplit_cost_hours > 0 and acc.observed_hours > 0:
            # Share of the observed hours whose cost had no CPU/RAM split and
            # was apportioned at the node's price ratio (F-03).
            assumptions["unsplit_cost_hours_share"] = round(acc.unsplit_cost_hours / acc.observed_hours, 4)
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
