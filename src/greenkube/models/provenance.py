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
    KUBECOST = "kubecost"
    PRICING_CATALOG = "pricing_catalog"
    CUSTOM_RATE = "custom_rate"
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


class IntensitySource(StrEnum):
    REALTIME = "realtime"
    CONSOLIDATED = "consolidated"
    STALE = "stale"
    ZONE_DEFAULT = "zone_default"
    GLOBAL_DEFAULT = "global_default"
    UNKNOWN = "unknown"
    """Legacy rows written before provenance was recorded."""
    MIXED = MIXED


#: Cost sources whose figures may back a measured cost saving (C0.3).
TRUSTED_COST_SOURCES: frozenset[str] = frozenset({CostSource.OPENCOST, CostSource.KUBECOST, CostSource.PRICING_CATALOG})
#: Cost sources that come from an allocation engine rather than list prices.
ALLOCATED_COST_SOURCES: frozenset[str] = frozenset({CostSource.OPENCOST, CostSource.KUBECOST})
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
    adjustments that must not be presented as savings).
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
    return math.isfinite(total_value) and total_value >= 0


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


def cost_source_for(cost_found: bool, default_cost: float) -> CostSource:
    """Provenance of an OpenCost-or-default cost lookup."""
    if cost_found:
        return CostSource.OPENCOST
    return CostSource.CUSTOM_RATE if default_cost else CostSource.NONE


__all__ = [
    "ALLOCATED_COST_SOURCES",
    "MEASURED_INTENSITY_SOURCES",
    "MEASURED_POWER_SOURCES",
    "MEASURED_USAGE_SOURCES",
    "MIXED",
    "PROVENANCE_FIELDS",
    "TRUSTED_COST_SOURCES",
    "TRUSTED_USAGE_SOURCES",
    "CostSource",
    "IntensitySource",
    "PowerSource",
    "UsageSource",
    "combine_sources",
    "cost_source_for",
    "is_cost_trusted",
    "is_intensity_measured",
    "is_power_profiled",
    "is_usage_trusted",
    "provenance_of",
]
