# src/greenkube/models/provenance.py
"""Per-dimension provenance of stored metric rows (roadmap A11).

Every pod and container metric row records *where* each of its dimensions
came from, replacing the single ``is_estimated`` boolean:

* ``cost_source``      — who priced the row (OpenCost, Kubecost, list prices, …);
* ``usage_source``     — where CPU/memory usage was measured;
* ``power_source``     — which node power profile converted usage to energy;
* ``intensity_source`` — how fresh the grid carbon intensity was.

Values are stored as plain strings so rows written by older versions (``None``)
and rows aggregated over mixed sources (``"mixed"``) stay readable. Consumers
declare which dimensions they need to trust through the helpers below instead
of reading ``is_estimated``.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any, Iterable, Optional

MIXED = "mixed"
"""Hourly aggregates of raw rows whose sources differ."""


class CostSource(StrEnum):
    OPENCOST = "opencost"
    OPENCOST_DERIVED = "opencost_derived"
    """OpenCost cost below its JSON precision, rebuilt from the allocation's resource-hours and
    OpenCost's own node unit prices (see ``collectors/opencost_collector.py``)."""
    KUBECOST = "kubecost"
    PRICING_CATALOG = "pricing_catalog"
    CUSTOM_RATE = "custom_rate"
    OPENCOST_PENDING = "opencost_pending"
    """Modeled cost of a window OpenCost could not price (outage, lag, rejected request): never
    trusted, re-priced from OpenCost by the collection job once it answers again (COST-01)."""
    NONE = "none"
    MIXED = MIXED


class UsageSource(StrEnum):
    PROMETHEUS = "prometheus"
    METRICS_SERVER = "metrics_server"
    NONE = "none"
    MIXED = MIXED


class PowerSource(StrEnum):
    INSTANCE_PROFILE = "instance_profile"
    NODE_CAPACITY_DERIVED = "node_capacity_derived"
    DEFAULT_PROFILE = "default_profile"
    KEPLER = "kepler"
    MIXED = MIXED


class EmbodiedSource(StrEnum):
    """Where the whole-node embodied emissions of a row came from (NRG-01)."""

    BOAVIZTA = "boavizta"
    """Boavizta API value for the node's cloud provider and instance type."""
    CONFIGURED = "configured"
    """Operator value: node annotation ``greenkube.io/embodied-kg``."""
    FALLBACK_PER_VCPU = "fallback_per_vcpu"
    """Estimate from the node capacity: ``DEFAULT_EMBODIED_KG_PER_VCPU`` × cores (+ per GiB)."""
    FALLBACK_DEFAULT = "fallback_default"
    """Flat ``DEFAULT_EMBODIED_EMISSIONS_KG``: neither a lookup nor the node capacity is known."""
    MIXED = MIXED


class GpuSource(StrEnum):
    """Where the GPU power of a row's node came from (NRG-13). NULL: no GPU detected on the node."""

    DCGM = "dcgm"
    """DCGM exporter samples (measured power, or TDP at the measured utilization)."""
    GPU_TDP_ESTIMATE = "gpu_tdp_estimate"
    """No DCGM sample: GPUs of the node capacity estimated from their TDP profile."""
    UNMEASURED = "unmeasured"
    """No DCGM sample and no TDP profile for the GPU model: GPU power is not counted."""


#: ``estimation_reasons`` prefix of rows whose embodied emissions are a fallback (NRG-01).
EMBODIED_FALLBACK_REASON_PREFIX = "Embodied emissions estimated"


class IntensitySource(StrEnum):
    REALTIME = "realtime"
    CONSOLIDATED = "consolidated"
    STALE = "stale"
    ZONE_DEFAULT = "zone_default"
    COUNTRY_DEFAULT = "country_default"
    """Bundled annual value of the zone's country: the zone has none of its own (NRG-04)."""
    GLOBAL_DEFAULT = "global_default"
    UNKNOWN = "unknown"
    """Legacy rows written before provenance was recorded."""
    MIXED = MIXED


class NodeSource(StrEnum):
    """Where the node attributes of a historical sample came from (WP-01.1, RECON-05)."""

    SNAPSHOT = "snapshot"
    """``node_snapshots_scd`` version valid at the sample time (or the earliest later one)."""
    CURRENT_API = "current_api"
    """The node list read from the Kubernetes API at collection time; no snapshot exists."""
    UNKNOWN = "unknown"
    """Neither: zone, PUE and profiles are configured defaults."""


class MetadataSource(StrEnum):
    """Where the pod requests, owner and containers of a historical sample came from (WP-01.1, COL-01)."""

    K8S_SNAPSHOT = "k8s_snapshot"
    """``pod_snapshots`` version valid at the sample time, the nearest one, or the live pod list."""
    KUBE_STATE_METRICS = "kube_state_metrics"
    """Per-step kube-state-metrics series (owner, container requests and limits) of a pod never observed live."""
    STORED_ROW = "stored_row"
    """Owner, requests and containers of the row already stored for this pod and step: a window re-collected
    after the pod and its history are gone re-runs the allocation with them."""
    MISSING = "missing"
    """No pod metadata at all: requests are not known, the owner is NULL."""
    REPAIRED = "repaired"
    """Owner and requests filled by migration 0270 from the nearest row of the same pod that had metadata
    (the last known values); the row's ``model_version`` is marked outdated so ``greenkube recompute``
    re-runs its allocation."""
    REPAIRED_PARTIAL = "repaired_partial"
    """Like ``repaired``, but the source row had requests and no owner: the owner is still unknown."""


#: Cost sources whose figures may back a measured cost saving (C0.3).
TRUSTED_COST_SOURCES: frozenset[str] = frozenset(
    {CostSource.OPENCOST, CostSource.OPENCOST_DERIVED, CostSource.KUBECOST, CostSource.PRICING_CATALOG}
)
#: Cost sources that come from an allocation engine rather than list prices.
ALLOCATED_COST_SOURCES: frozenset[str] = frozenset(
    {CostSource.OPENCOST, CostSource.OPENCOST_DERIVED, CostSource.KUBECOST}
)
#: Usage sources accepted by the measurement gates (C0.3).
TRUSTED_USAGE_SOURCES: frozenset[str] = frozenset({UsageSource.PROMETHEUS})
#: Usage sources that are measurements (time series or point-in-time).
MEASURED_USAGE_SOURCES: frozenset[str] = frozenset({UsageSource.PROMETHEUS, UsageSource.METRICS_SERVER})
#: Intensity sources backed by a provider value inside the staleness limit.
MEASURED_INTENSITY_SOURCES: frozenset[str] = frozenset({IntensitySource.REALTIME, IntensitySource.CONSOLIDATED})
#: Power sources that do not rely on the generic fallback profile.
MEASURED_POWER_SOURCES: frozenset[str] = frozenset(
    {PowerSource.INSTANCE_PROFILE, PowerSource.NODE_CAPACITY_DERIVED, PowerSource.KEPLER}
)

PROVENANCE_FIELDS: tuple[str, ...] = ("cost_source", "usage_source", "power_source", "intensity_source")

#: Start of the legacy ``estimation_reasons`` entry written when the carbon
#: intensity was a default or a stale value. The intensity recompute removes it
#: once a measured value replaces the default.
INTENSITY_FALLBACK_REASON_PREFIX = "Carbon intensity uses the "


def drop_intensity_fallback_reasons(reasons: Optional[Iterable[str]]) -> list[str]:
    """Return ``reasons`` without the intensity fallback entries."""
    return [reason for reason in reasons or [] if not str(reason).startswith(INTENSITY_FALLBACK_REASON_PREFIX)]


def _value(row: Any, field: str) -> Any:
    if isinstance(row, dict):
        return row.get(field)
    return getattr(row, field, None)


def _normalized(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return str(raw).strip().lower() or None


def is_cost_trusted(row: Any, *, allowed: Iterable[str] = TRUSTED_COST_SOURCES) -> bool:
    """Return whether a row's cost can back a measured cost figure.

    The row must name an allowed ``cost_source`` and carry a finite,
    non-negative ``total_cost`` (OpenCost occasionally returns negative
    adjustments that must not be presented as savings). A zero total on a row
    that requested CPU or memory is not trusted either: OpenCost had no price
    for it (a missing cost read as zero), and a zero after a change would be
    measured as the whole cost saved; the same holds for a row with CPU or
    memory usage (COST-05). A pod so small that OpenCost rounds its cost to
    zero is stored with its rebuilt cost (``opencost_derived``), not 0.
    """
    source = _normalized(_value(row, "cost_source"))
    if source is None or source not in {str(item) for item in allowed}:
        return False
    total = _value(row, "total_cost")
    if total is None:
        return True
    try:
        total_value = float(total)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(total_value) or total_value < 0:
        return False
    if total_value == 0 and _requested(row):
        return False
    return True


def _requested(row: Any) -> bool:
    """Whether a row requested or used CPU or memory (COST-05: a zero cost for it is a missing price).

    ``cpu_request`` (millicores), ``memory_request`` (bytes), and the usage
    columns ``cpu_usage_millicores`` and ``memory_usage_bytes``.
    """
    for field in ("cpu_request", "memory_request", "cpu_usage_millicores", "memory_usage_bytes"):
        try:
            value = float(_value(row, field) or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return True
    return False


def is_usage_trusted(row: Any, *, allowed: Iterable[str] = TRUSTED_USAGE_SOURCES) -> bool:
    """Return whether a row's CPU/memory usage is a time-series measurement."""
    source = _normalized(_value(row, "usage_source"))
    return source is not None and source in {str(item) for item in allowed}


def is_intensity_measured(row: Any) -> bool:
    """Return whether a row's carbon intensity came from a fresh provider value."""
    return _normalized(_value(row, "intensity_source")) in MEASURED_INTENSITY_SOURCES


def is_power_profiled(row: Any) -> bool:
    """Return whether a row's energy used a real (non-default) power profile."""
    return _normalized(_value(row, "power_source")) in MEASURED_POWER_SOURCES


def provenance_of(row: Any) -> dict[str, Optional[str]]:
    """Return the four provenance dimensions of a row as plain strings."""
    return {field: _normalized(_value(row, field)) for field in PROVENANCE_FIELDS}


def combine_sources(values: Iterable[Optional[str]]) -> Optional[str]:
    """Collapse the sources of several rows into one value (``mixed`` when they differ)."""
    seen = {_normalized(value) for value in values}
    if not seen:
        return None
    if len(seen) == 1:
        return next(iter(seen))
    return MIXED


#: Cost sources that may share an aggregate without making it ``mixed``: both are
#: OpenCost's own allocation, at its JSON precision or rebuilt below it.
OPENCOST_COST_FAMILY: frozenset[str] = frozenset({CostSource.OPENCOST.value, CostSource.OPENCOST_DERIVED.value})


def combine_cost_sources(values: Iterable[Optional[str]]) -> Optional[str]:
    """Collapse the ``cost_source`` of several rows into one (hourly and workload rollups).

    Like the other dimensions, a shared value is kept, no value gives
    ``None`` and anything else gives ``mixed`` (untrusted), including a group
    where some rows carry no source. The one exception: rows that are all
    ``opencost`` or ``opencost_derived`` give ``opencost_derived``. Both are
    trusted OpenCost costs; the aggregate names the derived label because part
    of its cost was rebuilt below OpenCost's precision, which provenance must
    not hide. :func:`cost_source_aggregate_sql` is the SQL form.
    """
    items = list(values)
    present = [_normalized(value) for value in items if value]
    if not present:
        return None
    if len(present) != len(items):
        return MIXED
    distinct = set(present)
    if len(distinct) == 1:
        return present[0]
    if distinct <= OPENCOST_COST_FAMILY:
        return CostSource.OPENCOST_DERIVED.value
    return MIXED


def cost_source_aggregate_sql(column: str = "cost_source") -> str:
    """SQL expression of :func:`combine_cost_sources` over a ``GROUP BY`` (SQLite and PostgreSQL)."""
    family = ", ".join(f"'{value}'" for value in sorted(OPENCOST_COST_FAMILY))
    return (
        f"CASE WHEN COUNT({column}) = 0 THEN NULL "
        f"WHEN COUNT({column}) <> COUNT(*) THEN '{MIXED}' "
        f"WHEN MIN({column}) = MAX({column}) THEN MIN({column}) "
        f"WHEN SUM(CASE WHEN {column} IN ({family}) THEN 0 ELSE 1 END) = 0 "
        f"THEN '{CostSource.OPENCOST_DERIVED.value}' "
        f"ELSE '{MIXED}' END"
    )


#: Quality order of cost sources, best first (STO-01): an allocation engine, OpenCost's rebuilt
#: cost, list prices, configured rates, then nothing. A re-collected row never replaces the cost
#: of a stored row with a strictly worse source. ``mixed`` (hourly rollups only) ranks with rates.
COST_SOURCE_RANK: dict[str, int] = {
    CostSource.OPENCOST.value: 5,
    CostSource.KUBECOST.value: 5,
    CostSource.OPENCOST_DERIVED.value: 4,
    CostSource.PRICING_CATALOG.value: 3,
    CostSource.CUSTOM_RATE.value: 2,
    CostSource.OPENCOST_PENDING.value: 2,
    CostSource.MIXED.value: 2,
    CostSource.NONE.value: 1,
}


def cost_source_rank(source: Optional[str]) -> int:
    """Rank of a cost source in :data:`COST_SOURCE_RANK` (0 when unknown or NULL)."""
    return COST_SOURCE_RANK.get(_normalized(source) or "", 0)


def cost_source_rank_sql(expression: str) -> str:
    """SQL form of :func:`cost_source_rank` (SQLite and PostgreSQL)."""
    cases = " ".join(f"WHEN '{source}' THEN {rank}" for source, rank in COST_SOURCE_RANK.items())
    return f"(CASE LOWER({expression}) {cases} ELSE 0 END)"


#: Price basis of a trusted cost: an allocation engine's own figure, or a list price (COST-01 acceptance d).
COST_BASIS_ALLOCATED = "allocated"
COST_BASIS_LIST_PRICE = "list_price"


def cost_basis_family(source: Optional[str]) -> Optional[str]:
    """Price basis of a cost source: ``allocated`` (OpenCost, Kubecost), ``list_price`` (catalog), else ``None``.

    A change measurement must not compare a baseline priced on one basis with
    an after-window priced on the other: the price switch would read as a saving.
    """
    value = _normalized(source)
    if value in ALLOCATED_COST_SOURCES:
        return COST_BASIS_ALLOCATED
    if value == CostSource.PRICING_CATALOG.value:
        return COST_BASIS_LIST_PRICE
    return None


def cost_source_for(cost_found: bool) -> CostSource:
    """Provenance of an OpenCost-or-default cost lookup.

    ``DEFAULT_COST`` is a flat placeholder per row, not a per core-hour or
    GiB-hour rate, so rows priced with it carry ``none`` (as migration 0039
    backfills legacy rows); ``custom_rate`` is reserved for configured rates.
    """
    return CostSource.OPENCOST if cost_found else CostSource.NONE


__all__ = [
    "ALLOCATED_COST_SOURCES",
    "INTENSITY_FALLBACK_REASON_PREFIX",
    "MEASURED_INTENSITY_SOURCES",
    "MEASURED_POWER_SOURCES",
    "MEASURED_USAGE_SOURCES",
    "MIXED",
    "OPENCOST_COST_FAMILY",
    "PROVENANCE_FIELDS",
    "TRUSTED_COST_SOURCES",
    "TRUSTED_USAGE_SOURCES",
    "CostSource",
    "IntensitySource",
    "PowerSource",
    "UsageSource",
    "combine_cost_sources",
    "combine_sources",
    "cost_source_aggregate_sql",
    "cost_source_for",
    "drop_intensity_fallback_reasons",
    "is_cost_trusted",
    "is_intensity_measured",
    "is_power_profiled",
    "is_usage_trusted",
    "provenance_of",
]
