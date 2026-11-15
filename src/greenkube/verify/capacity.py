# src/greenkube/verify/capacity.py
"""Measured released capacity and the capacity verdict of a change (WP-08.1).

A rightsizing changes reservations, and the reservation it releases can be
verified from the workload specs alone: no price, no OpenCost and no
autoscaler are needed. The measurement compares the requested capacity per
replica before and after the change, from the per-container requests the
applied detector records with each workload spec observation
(``workload_spec_observations.container_resources``), and takes replicas from
the hour buckets of the baseline and the post-window:

* ``per_replica``: requested cores and GiB per replica before minus after;
* ``total``: ``per_replica x`` the post-window mean replicas, the release
  booked for the change;
* ``replica_change``: when the mean replica count moved between the windows,
  the capacity that move reserved (or freed) at the new request, reported
  apart and never booked as a release;
* ``cpu_core_hours`` / ``mem_gib_hours``: the release accrued over the
  post-window, ``per_replica x`` the post-window replica-hours.

The verdict (``released``, ``partially_released``, ``not_released``) compares
the measured per-replica release with the release the change's
recommendations proposed. It never depends on the cost verdict, so it is
available when that one is ``inconclusive``. Released capacity is never
converted to money and never added to cost savings.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from greenkube.utils.time import ensure_utc

CAPACITY_VERSION = "capacity/1"

VERDICT_RELEASED = "released"
VERDICT_PARTIALLY_RELEASED = "partially_released"
VERDICT_NOT_RELEASED = "not_released"
#: The specs needed to measure the requests are missing (observations recorded before WP-08.1).
VERDICT_UNKNOWN = "unknown"

#: Share of the proposed per-replica release that must be observed for ``released``.
RELEASED_RATIO = 0.9
#: Below this share of the proposed release nothing was released (rounding of quantities).
NOT_RELEASED_RATIO = 0.05
#: Mean replica counts closer than this are the same replica count.
REPLICA_CHANGE_THRESHOLD = 0.5

BYTES_PER_GIB = 1024.0**3

UNITS = {
    "cpu_cores": "cores",
    "mem_gib": "GiB",
    "cpu_core_hours": "core-hours",
    "mem_gib_hours": "GiB-hours",
    "replicas": "replicas",
}

Requests = Dict[str, Tuple[Optional[float], Optional[float]]]


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _from_resources(resources: Mapping[str, Any]) -> Requests:
    """``{container: (cores, GiB)}`` of an observation's ``container_resources``."""
    requests: Requests = {}
    for name, values in resources.items():
        if not isinstance(values, Mapping):
            continue
        cpu = _number(values.get("cpu_request_millicores"))
        memory = _number(values.get("memory_request_bytes"))
        requests[str(name)] = (
            cpu / 1000.0 if cpu is not None else None,
            memory / BYTES_PER_GIB if memory is not None else None,
        )
    return requests


def _from_pairs(pairs: Mapping[str, Any]) -> Requests:
    """``{container: (cores, GiB)}`` of a change's ``details.container_requests`` (``[millicores, bytes]``)."""
    requests: Requests = {}
    for name, values in pairs.items():
        if not isinstance(values, (list, tuple)) or len(values) != 2:
            continue
        cpu, memory = _number(values[0]), _number(values[1])
        requests[str(name)] = (
            cpu / 1000.0 if cpu is not None else None,
            memory / BYTES_PER_GIB if memory is not None else None,
        )
    return requests


def _observed_at(observation: Any) -> Optional[datetime]:
    value = getattr(observation, "observed_at", None)
    return ensure_utc(value) if isinstance(value, datetime) else None


def spec_requests(
    change: Any, observations: Sequence[Any], post_end: datetime
) -> Tuple[Optional[Requests], str, Optional[Requests], str]:
    """Per-container requests before and after the change, with where each comes from.

    Before: the latest observation recorded at or before the merge
    (``baseline_window_end``). After: the latest observation recorded after
    it and before the post-window end; without one the spec did not change
    (the detector records every new revision), unless no ``before``
    observation exists, in which case the requests recorded with the change
    are used.
    """
    merge = ensure_utc(change.baseline_window_end)
    end = ensure_utc(post_end)
    stamped: list[Tuple[datetime, Any]] = []
    for item in observations:
        stamp = _observed_at(item)
        if stamp is not None:
            stamped.append((stamp, item))
    stamped.sort(key=lambda pair: pair[0])
    before_obs = [item for stamp, item in stamped if stamp <= merge]
    after_obs = [item for stamp, item in stamped if merge < stamp <= end]
    before: Optional[Requests] = None
    before_source = "missing"
    if before_obs:
        latest = before_obs[-1]
        resources = getattr(latest, "container_resources", None)
        if isinstance(resources, Mapping) and resources:
            before, before_source = _from_resources(resources), "spec_observation"
    after: Optional[Requests] = None
    after_source = "missing"
    with_resources = [item for item in after_obs if isinstance(getattr(item, "container_resources", None), Mapping)]
    if with_resources:
        after, after_source = _from_resources(with_resources[-1].container_resources), "spec_observation"
    elif not after_obs and before is not None:
        after, after_source = dict(before), "spec_observation_unchanged"
    else:
        recorded = (getattr(change, "details", None) or {}).get("container_requests")
        if isinstance(recorded, Mapping) and recorded:
            after, after_source = _from_pairs(recorded), "change_record"
    return before, before_source, after, after_source


def _per_replica(requests: Requests, containers: Sequence[str]) -> Tuple[Optional[float], Optional[float]]:
    names = [name for name in containers if name in requests] if containers else list(requests)
    if not names:
        return None, None

    def total(values: list) -> Optional[float]:
        # A container without a request reserves nothing for that resource (0), as the scheduler sees it.
        present = [value for value in values if value is not None]
        return math.fsum(present) if present else None

    return total([requests[name][0] for name in names]), total([requests[name][1] for name in names])


def mean_replicas(buckets: Sequence[Any]) -> Optional[float]:
    """Mean replica count over the buckets that report one (``None`` without any)."""
    values = [_number(getattr(bucket, "replicas", None)) for bucket in buckets]
    present = [value for value in values if value is not None]
    return math.fsum(present) / len(present) if present else None


def replica_hours(buckets: Sequence[Any], slot_seconds: int = 3600) -> Tuple[Optional[float], float]:
    """Replica-hours and covered hours of ``buckets``."""
    values = [_number(getattr(bucket, "replicas", None)) for bucket in buckets]
    present = [value for value in values if value is not None]
    hours = len(present) * slot_seconds / 3600.0
    return (math.fsum(present) * slot_seconds / 3600.0 if present else None), hours


def proposed_release(members: Sequence[Any]) -> Tuple[Optional[float], Optional[float]]:
    """Per-replica release the recommendations proposed (cores, GiB); negative for increases."""
    cpu: Optional[float] = None
    mem: Optional[float] = None
    for member in members:
        current = _number(getattr(member, "current_cpu_request_millicores", None))
        proposed = _number(getattr(member, "actual_cpu_request_millicores", None))
        if proposed is None:
            proposed = _number(getattr(member, "recommended_cpu_request_millicores", None))
        if current is not None and proposed is not None:
            cpu = (cpu or 0.0) + (current - proposed) / 1000.0
        current = _number(getattr(member, "current_memory_request_bytes", None))
        proposed = _number(getattr(member, "actual_memory_request_bytes", None))
        if proposed is None:
            proposed = _number(getattr(member, "recommended_memory_request_bytes", None))
        if current is not None and proposed is not None:
            mem = (mem or 0.0) + (current - proposed) / BYTES_PER_GIB
    return cpu, mem


def capacity_verdict(
    measured: Tuple[Optional[float], Optional[float]], proposed: Tuple[Optional[float], Optional[float]]
) -> Tuple[Optional[str], str]:
    """Verdict and reason from the measured and proposed per-replica releases (cores, GiB).

    ``None`` for an increase: more capacity is reserved, which is never a release.
    """
    pairs = [(m, p) for m, p in zip(measured, proposed) if m is not None]
    if not pairs:
        return VERDICT_UNKNOWN, "the requests before or after the change were not recorded"
    expected = [(m, p) for m, p in pairs if p is not None and abs(p) > 1e-9]
    if expected and all(p < 0 for _, p in expected):
        return None, "an increase reserves more capacity: it is not a release"
    releases = [(m, p) for m, p in expected if p > 0]
    if releases:
        ratios = [m / p for m, p in releases]
        if all(ratio >= RELEASED_RATIO for ratio in ratios):
            return VERDICT_RELEASED, "the proposed per-replica request decrease is in effect"
        if all(ratio < NOT_RELEASED_RATIO for ratio in ratios):
            return VERDICT_NOT_RELEASED, "the per-replica requests did not decrease"
        return VERDICT_PARTIALLY_RELEASED, (
            "the per-replica requests decreased by "
            + ", ".join(f"{ratio:.0%}" for ratio in ratios)
            + " of the proposed decrease"
        )
    values = [m for m, _ in pairs]
    if any(value > 1e-9 for value in values) and not any(value < -1e-9 for value in values):
        return VERDICT_RELEASED, "the per-replica requests decreased (no proposal to compare with)"
    return VERDICT_NOT_RELEASED, "the per-replica requests did not decrease"


def measure_capacity(
    change: Any,
    observations: Sequence[Any],
    pre_buckets: Sequence[Any],
    post_buckets: Sequence[Any],
    members: Sequence[Any],
    *,
    post_end: datetime,
    slot_seconds: int = 3600,
) -> Dict[str, Any]:
    """The measured released capacity of a change and its verdict (see the module docstring)."""
    before, before_source, after, after_source = spec_requests(change, observations, post_end)
    containers = list(getattr(change, "containers", None) or [])
    before_cpu, before_mem = _per_replica(before, containers) if before is not None else (None, None)
    after_cpu, after_mem = _per_replica(after, containers) if after is not None else (None, None)
    released_cpu = before_cpu - after_cpu if before_cpu is not None and after_cpu is not None else None
    released_mem = before_mem - after_mem if before_mem is not None and after_mem is not None else None

    replicas_before = mean_replicas(pre_buckets)
    replicas_after = mean_replicas(post_buckets)
    post_replica_hours, post_hours = replica_hours(post_buckets, slot_seconds)

    def times(value: Optional[float], factor: Optional[float]) -> Optional[float]:
        return value * factor if value is not None and factor is not None else None

    replica_change = None
    if (
        replicas_before is not None
        and replicas_after is not None
        and abs(replicas_after - replicas_before) >= REPLICA_CHANGE_THRESHOLD
    ):
        delta = replicas_after - replicas_before
        replica_change = {
            "before": replicas_before,
            "after": replicas_after,
            "delta": delta,
            # More replicas reserve more: a negative release at the new per-replica request.
            "cpu_cores": -delta * after_cpu if after_cpu is not None else None,
            "mem_gib": -delta * after_mem if after_mem is not None else None,
            "note": "Replica change (HPA or manual scaling): reported apart, never booked as a release.",
        }

    proposed = proposed_release(members)
    verdict, reason = capacity_verdict((released_cpu, released_mem), proposed)
    direction = (
        "increase"
        if verdict is None
        else "release"
        if any(value is not None and value > 1e-9 for value in (released_cpu, released_mem))
        else "none"
    )
    return {
        "method": "measured",
        "version": CAPACITY_VERSION,
        "verdict": verdict,
        "reason": reason,
        "direction": direction,
        "requests_source": {"before": before_source, "after": after_source},
        "requested_per_replica": {
            "before": {"cpu_cores": before_cpu, "mem_gib": before_mem},
            "after": {"cpu_cores": after_cpu, "mem_gib": after_mem},
        },
        "per_replica": {"cpu_cores": released_cpu, "mem_gib": released_mem},
        "proposed_per_replica": {"cpu_cores": proposed[0], "mem_gib": proposed[1]},
        "replicas": {"before": replicas_before, "after": replicas_after},
        "total": {"cpu_cores": times(released_cpu, replicas_after), "mem_gib": times(released_mem, replicas_after)},
        "replica_change": replica_change,
        "post_window_hours": post_hours,
        "post_replica_hours": post_replica_hours,
        "cpu_core_hours": times(released_cpu, post_replica_hours),
        "mem_gib_hours": times(released_mem, post_replica_hours),
        "units": dict(UNITS),
        "note": "Reserved capacity, not money: never added to cost savings. Negative values are increases.",
    }
