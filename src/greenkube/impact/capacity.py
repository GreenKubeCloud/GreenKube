# src/greenkube/impact/capacity.py
"""Released capacity, the third quantity next to cost and CO2e (WP-08.1).

A rightsizing changes reservations. The capacity it releases is exact, needs
no price, no OpenCost and no autoscaler, and is the honest result on a
fixed-capacity cluster: the bill does not move, but capacity is freed.

Rules (docs/methodology/impact-model.md, "Released capacity"):

* released capacity is never converted to money and never added to cost
  savings: it is reported in its own units;
* an increase is a negative release, never shown as a saving;
* scope: CPU requests (cores, core-hours) and memory requests (GiB,
  GiB-hours) for request changes, storage (GiB) and LoadBalancer count only
  for deletion actions, no network figure.

This module builds the *projected* view of a recommendation from the stored
``impact.capacity_released`` and the current and proposed requests. The
measured view is built by :mod:`greenkube.verify.capacity`.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

HOURS_PER_YEAR = 8760.0
BYTES_PER_GIB = 1024.0**3

#: Units of every released-capacity figure (never a currency).
CAPACITY_UNITS: dict[str, str] = {
    "cpu_core_hours": "core-hours per year",
    "mem_gib_hours": "GiB-hours per year",
    "replica_hours": "replica-hours per year",
    "node_hours": "node-hours per year",
    "cpu_cores": "cores",
    "mem_gib": "GiB",
    "storage_gib": "GiB",
    "load_balancers": "count",
}

#: Direction of a capacity change: what the UI and the pull request call it.
DIRECTION_RELEASE = "release"
DIRECTION_INCREASE = "increase"
DIRECTION_MIXED = "mixed"
DIRECTION_NONE = "none"

_ANNUAL_KEYS = ("cpu_core_hours", "mem_gib_hours", "replica_hours", "node_hours")
_EPSILON = 1e-9


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def capacity_direction(*values: Optional[float]) -> str:
    """``release`` when every non-zero value is positive, ``increase`` when every one is negative."""
    signs = {1 if value > 0 else -1 for value in values if value is not None and abs(value) > _EPSILON}
    if not signs:
        return DIRECTION_NONE
    if signs == {1}:
        return DIRECTION_RELEASE
    if signs == {-1}:
        return DIRECTION_INCREASE
    return DIRECTION_MIXED


def _request_pair(record: Any, flat_current: str, flat_proposed: str, evidence_key: str) -> tuple:
    current = getattr(record, flat_current, None)
    proposed = getattr(record, flat_proposed, None)
    evidence = getattr(record, "evidence", None)
    if evidence is not None:
        if current is None:
            current = getattr(getattr(evidence, "current", None), evidence_key, None)
        if proposed is None:
            proposed = getattr(getattr(evidence, "proposed", None), evidence_key, None)
    return _number(current), _number(proposed)


def projected_capacity(record: Any) -> Optional[dict[str, Any]]:
    """Projected released capacity of a recommendation, or ``None`` when it has no projection.

    ``annual`` is the stored ``impact.capacity_released`` (per year, annualized
    over the target's observed coverage, so it inherits the coverage bias the
    Impact Model documents). ``instantaneous`` is ``(current request -
    proposed request) x replicas`` in cores and GiB, which needs no period;
    the replica count is the mean over the observed coverage
    (``replica_hours / 8760``). Negative values are increases.
    """
    impact = getattr(record, "impact", None)
    if not isinstance(impact, Mapping):
        return None
    stored = impact.get("capacity_released")
    if not isinstance(stored, Mapping):
        return None
    annual: dict[str, Any] = {key: _number(stored.get(key)) or 0.0 for key in _ANNUAL_KEYS}

    instantaneous: dict[str, Any] = {}
    cpu_current, cpu_proposed = _request_pair(
        record, "current_cpu_request_millicores", "recommended_cpu_request_millicores", "cpu_request_millicores"
    )
    mem_current, mem_proposed = _request_pair(
        record, "current_memory_request_bytes", "recommended_memory_request_bytes", "memory_request_bytes"
    )
    replica_hours = annual["replica_hours"]
    replicas = replica_hours / HOURS_PER_YEAR if replica_hours > 0 else None
    if cpu_current is not None and cpu_proposed is not None:
        per_replica = (cpu_current - cpu_proposed) / 1000.0
        instantaneous["cpu_cores_per_replica"] = per_replica
        instantaneous["cpu_cores"] = per_replica * replicas if replicas is not None else None
    if mem_current is not None and mem_proposed is not None:
        per_replica = (mem_current - mem_proposed) / BYTES_PER_GIB
        instantaneous["mem_gib_per_replica"] = per_replica
        instantaneous["mem_gib"] = per_replica * replicas if replicas is not None else None
    if instantaneous:
        instantaneous["replicas"] = replicas
        instantaneous["replicas_basis"] = "mean replicas over the observed coverage (replica_hours / 8760)"
    storage = _number(stored.get("storage_gib"))
    if storage:
        instantaneous["storage_gib"] = storage
    load_balancers = _number(stored.get("load_balancers"))
    if load_balancers:
        instantaneous["load_balancers"] = load_balancers

    direction = capacity_direction(
        annual["cpu_core_hours"],
        annual["mem_gib_hours"],
        instantaneous.get("cpu_cores_per_replica"),
        instantaneous.get("mem_gib_per_replica"),
        storage,
        load_balancers,
        annual["node_hours"],
    )
    return {
        "method": "projected",
        "label": "Released capacity (projected)",
        "period": "year",
        "basis": "annualized over the target's observed coverage (Impact Model)",
        "annual": annual,
        "instantaneous": instantaneous or None,
        "units": dict(CAPACITY_UNITS),
        "direction": direction,
        "note": (
            "Reserved capacity, not money: never added to cost savings. Negative values are increases."
            if direction != DIRECTION_RELEASE
            else "Reserved capacity, not money: never added to cost savings."
        ),
    }
