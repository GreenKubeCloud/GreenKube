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
    observed_cost_sources: Counter = field(default_factory=Counter)

    def track(self, node: NodeImpactProfile, sample: ResourceSample, pue: float, intensity: float) -> None:
        self.nodes.setdefault(node.name, node)
        weight = max(sample.hours, 0.0)
        self.weight_hours += weight
        self.confidence_weighted += price_confidence(node) * weight
        self.pue_weighted += pue * weight
        self.intensity_weighted += intensity * weight

    def add_cost(self, cost: CostBreakdown) -> None:
        self.cost = self.cost + cost

    def add_co2e(self, idle: float = 0.0, dynamic: float = 0.0, embodied: float = 0.0) -> None:
        self.co2e = Co2eBreakdown(
            idle_operational=self.co2e.idle_operational + idle,
            dynamic_operational=self.co2e.dynamic_operational + dynamic,
            embodied=self.co2e.embodied + embodied,
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
            delta = self._project_sample_removal(
                baseline, removed, ChangeKind.REPLICA_SCHEDULE, pool, pool.realization_factor, pool.realization_source
            )
            delta.assumptions["removed_hours_of_day"] = sorted(change.removed_hours_of_day)
            return delta
        if isinstance(change, AssetDeletion):
            return self._project_asset_deletion(change, pool)
        if isinstance(change, NodePoolChange):
            return self._project_node_pool(change, pool)
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
            acc.track(node, sample, pue, intensity)
            hours = max(sample.hours, 0.0)
            acc.replica_hours += hours

            cpu_released = 0.0
            if change.cpu_request_cores is not None:
                current, proposed = change.cpu_request_cores
                cpu_released = _released(current, proposed, sample.cpu_usage_cores)
            mem_released = 0.0
            if change.mem_request_gib is not None:
                current, proposed = change.mem_request_gib
                mem_released = _released(current, proposed, sample.mem_usage_gib)

            acc.cpu_core_hours += cpu_released * hours
            acc.mem_gib_hours += mem_released * hours
            acc.add_cost(
                CostBreakdown(
                    cpu=cpu_released * hours * node.cpu_price_per_reserved_core_hour,
                    ram=mem_released * hours * node.ram_price_per_reserved_gib_hour,
                )
            )
            # Request cuts do not change usage, so dynamic power is unchanged:
            # only the reservation-allocated idle power and embodied share move.
            idle_kwh = cpu_released * hours * node.idle_watts_per_reserved_core / 1000.0
            acc.add_co2e(
                idle=idle_kwh * pue * intensity,
                embodied=cpu_released * hours * node.embodied_g_per_reserved_core_hour,
            )

        delta = self._finalize(
            ChangeKind.CONTAINER_RESOURCES,
            acc,
            baseline.window_hours,
            pool,
            realization_factor=pool.realization_factor,
            co2e_basis="idle_and_embodied_share_released",
        )
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
            acc.track(node, sample, pue, intensity)
            hours = max(sample.hours, 0.0)
            acc.replica_hours += hours

            cpu_reserved = sample.cpu_reserved_cores
            mem_reserved = sample.mem_reserved_gib
            acc.cpu_core_hours += cpu_reserved * hours
            acc.mem_gib_hours += mem_reserved * hours
            acc.add_cost(_sample_allocated_cost(sample, node, acc))

            used = max(sample.cpu_usage_cores or 0.0, 0.0)
            idle_kwh = cpu_reserved * hours * node.idle_watts_per_reserved_core / 1000.0
            dynamic_kwh = used * hours * node.dynamic_watts_per_used_core / 1000.0
            acc.add_co2e(
                idle=idle_kwh * pue * intensity,
                dynamic=dynamic_kwh * pue * intensity,
                embodied=cpu_reserved * hours * node.embodied_g_per_reserved_core_hour,
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

    def _project_node_pool(self, change: NodePoolChange, pool: NodePoolContext) -> ImpactDelta:
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
            acc.nodes.setdefault(node.name, node)
            acc.weight_hours += hours
            acc.confidence_weighted += price_confidence(node) * hours
            acc.pue_weighted += node.pue * hours
            acc.intensity_weighted += node.grid_intensity_g_per_kwh * hours
            acc.cpu_core_hours += node.cpu_capacity_cores * hours
            acc.mem_gib_hours += node.mem_capacity_gib * hours
            acc.add_cost(
                CostBreakdown(
                    cpu=node.cpu_capacity_cores * node.prices.cpu_core_hour * hours,
                    ram=node.mem_capacity_gib * node.prices.ram_gib_hour * hours,
                )
            )
            # Workloads move to the remaining nodes, so dynamic power stays;
            # the removed node's idle power and embodied emissions go away.
            idle_kwh = node.idle_watts * hours / 1000.0
            acc.add_co2e(
                idle=idle_kwh * node.pue * node.grid_intensity_g_per_kwh,
                embodied=node.embodied_g_per_hour * hours,
            )

        realization_factor = 1.0 if change.action_is_consolidation else pool.realization_factor
        delta = self._finalize(
            ChangeKind.NODE_POOL,
            acc,
            float(HOURS_PER_YEAR),
            pool,
            realization_factor=realization_factor,
            realization_source="consolidation_action" if change.action_is_consolidation else None,
            co2e_basis="node_idle_and_embodied_removed",
            node_hours=node_hours,
        )
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
        currencies = sorted({node.prices.currency for node in acc.nodes.values()}) or [pool.currency]
        currency = currencies[0] if len(currencies) == 1 else "mixed"

        assumptions: Dict[str, Any] = {
            "allocation_policy": "reservation_aware",
            "window_hours": window_hours,
            "annualization_factor": factor,
            "unit_prices": _listed_prices(acc.nodes),
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


def _utc_hour(timestamp: datetime) -> int:
    """Hour of day in UTC; naive timestamps are already UTC in GreenKube storage."""
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.hour


def _released(current: float, proposed: float, usage: Optional[float]) -> float:
    """Reservation released by moving a request, under ``max(request, usage)`` reservation."""
    used = max(usage or 0.0, 0.0)
    return max(current, used, 0.0) - max(proposed, used, 0.0)


def _sample_environment(sample: ResourceSample, node: NodeImpactProfile) -> Tuple[float, float]:
    """PUE and grid intensity of a sample, falling back to the node profile."""
    pue = sample.pue if sample.pue is not None and sample.pue > 0 else node.pue
    intensity = (
        sample.grid_intensity_g_per_kwh
        if sample.grid_intensity_g_per_kwh is not None and sample.grid_intensity_g_per_kwh > 0
        else node.grid_intensity_g_per_kwh
    )
    return pue, intensity


def _sample_allocated_cost(sample: ResourceSample, node: NodeImpactProfile, acc: _Accumulator) -> CostBreakdown:
    """Current allocated cost of a sample: observed when known, otherwise modeled."""
    total = sample.observed_cost_total
    if total is not None and total > 0:
        acc.observed_cost_sources[sample.observed_cost_source or CostSource.OPENCOST.value] += 1
        components = {
            "cpu": max(sample.observed_cost_cpu or 0.0, 0.0),
            "ram": max(sample.observed_cost_ram or 0.0, 0.0),
            "storage": max(sample.observed_cost_storage or 0.0, 0.0),
            "network": max(sample.observed_cost_network or 0.0, 0.0),
            "lb": max(sample.observed_cost_lb or 0.0, 0.0),
        }
        split = sum(components.values())
        if split > total:
            # Inconsistent split: trust the total and scale the components down.
            components = {name: value * total / split for name, value in components.items()}
            split = total
        return CostBreakdown(other=total - split, **components)
    hours = max(sample.hours, 0.0)
    return CostBreakdown(
        cpu=sample.cpu_reserved_cores * hours * node.cpu_price_per_reserved_core_hour,
        ram=sample.mem_reserved_gib * hours * node.ram_price_per_reserved_gib_hour,
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
