# src/greenkube/verify/series.py
"""Hourly aggregation of a workload's metric rows (C1, C3).

Both the baseline snapshot and the post-change observations are reduced to one
:class:`~greenkube.verify.model.HourBucket` per UTC clock hour, using the same
trust rules as the verifier: cost only from rows with trusted cost provenance
(C0.3), carbon only from rows with measured intensity and a profiled node.
Rows are grouped per timestamp (the replicas of one sample); a timestamp
group counts towards cost (or carbon) only when every row in it is trusted.
Rates divide by the covered time, so raw 5-minute rows and hourly rollups
mix correctly. Embodied CO2e (``embodied_co2e_grams``, allocated by
reservation) is aggregated apart from the operational CO2e: it needs no
measured intensity.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from greenkube.core.optimization.verifier import (
    _coverage_seconds,
    carbon_is_trusted,
    cost_is_trusted,
    usage_is_trusted,
)
from greenkube.utils.time import ensure_utc
from greenkube.verify.model import HourBucket, TimeWindow

_GIB = 1024.0**3
_JOULES_PER_KWH = 3_600_000.0


def floor_hour(moment: datetime) -> datetime:
    moment = ensure_utc(moment)
    return moment.replace(minute=0, second=0, microsecond=0)


def window_hours(window: TimeWindow) -> List[datetime]:
    """Every UTC clock hour starting inside ``window``."""
    hours: List[datetime] = []
    current = floor_hour(window.start)
    if current < ensure_utc(window.start):
        current += timedelta(hours=1)
    end = ensure_utc(window.end)
    while current < end:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


class _Accumulator:
    __slots__ = (
        "covered",
        "pod_seconds",
        "cpu_request",
        "memory_request",
        "cpu_charged",
        "memory_charged",
        "cpu_usage",
        "usage_seconds",
        "cost",
        "cost_cpu",
        "cost_ram",
        "cost_seconds",
        "components",
        "energy_joules",
        "co2e",
        "co2e_seconds",
        "embodied",
        "embodied_seconds",
        "instance_types",
    )

    def __init__(self) -> None:
        self.covered = 0.0
        self.pod_seconds = 0.0
        self.cpu_request = 0.0
        self.memory_request = 0.0
        self.cpu_charged = 0.0
        self.memory_charged = 0.0
        self.cpu_usage = 0.0
        self.usage_seconds = 0.0
        self.cost = 0.0
        self.cost_cpu = 0.0
        self.cost_ram = 0.0
        self.cost_seconds = 0.0
        self.components = True
        self.energy_joules = 0.0
        self.co2e = 0.0
        self.co2e_seconds = 0.0
        self.embodied = 0.0
        self.embodied_seconds = 0.0
        self.instance_types: Dict[str, float] = defaultdict(float)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def hourly_buckets(rows: Iterable[Any], window: TimeWindow, step_seconds: int) -> List[HourBucket]:
    """Aggregate pod-level rows of one workload into hour buckets inside ``window``.

    Every hour of the window is returned, with ``coverage == 0`` when it has no
    data, so callers can compute coverage against the full window.
    """
    start = ensure_utc(window.start)
    end = ensure_utc(window.end)
    groups: Dict[datetime, List[Any]] = defaultdict(list)
    for row in rows:
        timestamp = getattr(row, "timestamp", None)
        if timestamp is None:
            continue
        moment = ensure_utc(timestamp)
        if moment < start or moment >= end:
            continue
        groups[moment].append(row)

    accumulators: Dict[datetime, _Accumulator] = defaultdict(_Accumulator)
    for moment, members in groups.items():
        acc = accumulators[floor_hour(moment)]
        coverage = max(_coverage_seconds(row, step_seconds) for row in members)
        acc.covered += coverage
        for row in members:
            seconds = _coverage_seconds(row, step_seconds)
            acc.pod_seconds += seconds
            cpu_request = float(getattr(row, "cpu_request", 0) or 0) / 1000.0
            memory_request = float(getattr(row, "memory_request", 0) or 0) / _GIB
            acc.cpu_request += cpu_request * seconds
            acc.memory_request += memory_request * seconds
            trusted_usage = usage_is_trusted(row)
            usage = _number(getattr(row, "cpu_usage_millicores", None))
            memory_usage = _number(getattr(row, "memory_usage_bytes", None))
            # The charged quantity: max(request, usage) per sample (MEA-02); the request without trusted usage.
            cpu_used = usage / 1000.0 if usage is not None and usage >= 0 and trusted_usage else None
            memory_used = (
                memory_usage / _GIB if memory_usage is not None and memory_usage >= 0 and trusted_usage else None
            )
            acc.cpu_charged += max(cpu_request, cpu_used or 0.0) * seconds
            acc.memory_charged += max(memory_request, memory_used or 0.0) * seconds
            if cpu_used is not None:
                acc.cpu_usage += cpu_used * seconds
                acc.usage_seconds += seconds
            acc.energy_joules += float(getattr(row, "joules", 0.0) or 0.0)
            instance_type = getattr(row, "node_instance_type", None)
            if instance_type:
                acc.instance_types[str(instance_type)] += seconds / 3600.0
        if all(cost_is_trusted(row) for row in members):
            acc.cost += sum(float(getattr(row, "total_cost", 0.0) or 0.0) for row in members)
            acc.cost_seconds += coverage
            cpu = [getattr(row, "cost_cpu", None) for row in members]
            ram = [getattr(row, "cost_ram", None) for row in members]
            if any(value is None for value in cpu + ram):
                acc.components = False
            else:
                acc.cost_cpu += sum(float(value) for value in cpu if value is not None)
                acc.cost_ram += sum(float(value) for value in ram if value is not None)
        if all(carbon_is_trusted(row) for row in members):
            acc.co2e += sum(float(getattr(row, "co2e_grams", 0.0) or 0.0) for row in members)
            acc.co2e_seconds += coverage
        embodied = [_number(getattr(row, "embodied_co2e_grams", None)) for row in members]
        if all(value is not None and value >= 0 for value in embodied):
            # Embodied emissions are allocated by the Impact Model by reservation; they do not
            # depend on the grid intensity, so they need no measured intensity.
            acc.embodied += math.fsum(value for value in embodied if value is not None)
            acc.embodied_seconds += coverage

    buckets: List[HourBucket] = []
    for hour in window_hours(TimeWindow(start=start, end=end)):
        acc = accumulators.get(hour)
        if acc is None or acc.covered <= 0:
            buckets.append(HourBucket(hour=hour))
            continue
        covered = acc.covered
        per_hour = 3600.0 / covered
        has_components = acc.components and acc.cost_seconds > 0 and (acc.cost_cpu + acc.cost_ram > 0 or acc.cost <= 0)
        buckets.append(
            HourBucket(
                hour=hour,
                coverage=min(covered / 3600.0, 1.0),
                replicas=acc.pod_seconds / covered,
                core_hours=acc.cpu_request / covered,
                gib_hours=acc.memory_request / covered,
                charged_core_hours=acc.cpu_charged / covered,
                charged_gib_hours=acc.memory_charged / covered,
                cpu_usage_cores=acc.cpu_usage / acc.usage_seconds * (acc.pod_seconds / covered)
                if acc.usage_seconds > 0
                else None,
                cost=acc.cost / acc.cost_seconds * 3600.0 if acc.cost_seconds > 0 else None,
                cost_cpu=acc.cost_cpu / acc.cost_seconds * 3600.0 if has_components else None,
                cost_ram=acc.cost_ram / acc.cost_seconds * 3600.0 if has_components else None,
                cost_coverage=min(acc.cost_seconds / 3600.0, 1.0),
                energy_kwh=acc.energy_joules / _JOULES_PER_KWH * per_hour,
                co2e_g=acc.co2e / acc.co2e_seconds * 3600.0 if acc.co2e_seconds > 0 else None,
                co2e_coverage=min(acc.co2e_seconds / 3600.0, 1.0),
                embodied_co2e_g=acc.embodied / acc.embodied_seconds * 3600.0 if acc.embodied_seconds > 0 else None,
                embodied_coverage=min(acc.embodied_seconds / 3600.0, 1.0),
                instance_types={key: value * per_hour for key, value in acc.instance_types.items()},
            )
        )
    return buckets


#: HourBucket attribute backing each measured quantity, and the coverage that qualifies it.
QUANTITY_FIELDS: Dict[str, tuple[str, str]] = {
    "allocated_cost": ("cost", "cost_coverage"),
    "allocated_cost_observed": ("cost", "cost_coverage"),
    "cost_cpu": ("cost_cpu", "cost_coverage"),
    "cost_ram": ("cost_ram", "cost_coverage"),
    "core_hours": ("core_hours", "coverage"),
    "gib_hours": ("gib_hours", "coverage"),
    "co2e_g": ("co2e_g", "co2e_coverage"),
    "embodied_co2e_g": ("embodied_co2e_g", "embodied_coverage"),
    "replicas": ("replicas", "coverage"),
    "cpu_usage_cores": ("cpu_usage_cores", "coverage"),
}

#: Minimum share of an hour with qualifying data for the hour to count.
MIN_HOUR_COVERAGE = 0.5


#: Cost, charged quantity and requested quantity of each priced resource (MEA-02).
PRICED_RESOURCES: Dict[str, tuple[str, str, str]] = {
    "cpu": ("cost_cpu", "charged_core_hours", "core_hours"),
    "memory": ("cost_ram", "charged_gib_hours", "gib_hours"),
}


def has_charged(buckets: Sequence[HourBucket]) -> bool:
    """Whether every costed hour of ``buckets`` carries the charged quantities (at least one does)."""
    costed = [bucket for bucket in buckets if bucket.cost is not None and bucket.cost_coverage >= MIN_HOUR_COVERAGE]
    return bool(costed) and all(
        bucket.charged_core_hours is not None and bucket.charged_gib_hours is not None for bucket in costed
    )


def unit_prices(buckets: Sequence[HourBucket], *, charged: bool) -> Dict[str, Optional[float]]:
    """Allocated cost per charged (or requested) core-hour and GiB-hour over ``buckets``.

    Only hours with trusted cost components on at least half the hour count.
    With ``charged`` the quantity is ``max(request, usage)`` per sample, so the
    price of a resource stays a price even when the change resized it below its
    usage (an allocator that charges usage then charges the same quantity).
    """
    prices: Dict[str, Optional[float]] = {}
    for resource, (cost_attr, charged_attr, requested_attr) in PRICED_RESOURCES.items():
        cost = quantity = 0.0
        for bucket in buckets:
            value = getattr(bucket, cost_attr)
            amount = getattr(bucket, charged_attr if charged else requested_attr)
            if value is None or amount is None or amount <= 0 or bucket.cost_coverage < MIN_HOUR_COVERAGE:
                continue
            cost += value
            quantity += amount
        prices[resource] = cost / quantity if quantity > 0 else None
    return prices


def price_card(buckets: Sequence[HourBucket], currency: Optional[str]) -> Dict[str, Any]:
    """Unit prices of a baseline window, frozen with its snapshot.

    ``cpu_core_hour``/``memory_gib_hour`` are per requested unit (kept as they
    were first frozen); ``cpu_charged_core_hour``/``memory_charged_gib_hour``
    are per charged unit, the prices the request effect is valued at (MEA-02),
    present only when every costed hour carries the charged quantities.
    """
    card: Dict[str, Any] = {"currency": currency}
    for name, cost_attr, quantity_attr in (
        ("cpu_core_hour", "cost_cpu", "core_hours"),
        ("memory_gib_hour", "cost_ram", "gib_hours"),
    ):
        cost = quantity = 0.0
        for bucket in buckets:
            value = getattr(bucket, cost_attr)
            amount = getattr(bucket, quantity_attr)
            if value is None or amount is None or amount <= 0:
                continue
            cost += value
            quantity += amount
        card[name] = cost / quantity if quantity > 0 else None
    if has_charged(buckets):
        charged = unit_prices(buckets, charged=True)
        card["cpu_charged_core_hour"] = charged["cpu"]
        card["memory_charged_gib_hour"] = charged["memory"]
    return card


def has_embodied(buckets: Sequence[HourBucket]) -> bool:
    """Whether ``buckets`` measured embodied CO2e.

    Snapshots frozen before embodied emissions were measured carry none, and a
    window whose embodied CO2e is zero everywhere had no hardware profile (the
    Impact Model writes 0 when the node's embodied emissions are unknown).
    """
    return any(bucket.embodied_co2e_g is not None and bucket.embodied_co2e_g > 0 for bucket in buckets)


def co2e_series(buckets: Sequence[HourBucket], *, embodied: bool) -> Dict[datetime, float]:
    """Operational CO2e per hour, plus embodied CO2e when ``embodied`` (hours with both only)."""
    operational = quantity_series(buckets, "co2e_g")
    if not embodied:
        return operational
    manufactured = quantity_series(buckets, "embodied_co2e_g")
    return {hour: value + manufactured[hour] for hour, value in operational.items() if hour in manufactured}


def quantity_series(buckets: Sequence[HourBucket], quantity: str) -> Dict[datetime, float]:
    """``{hour: value}`` of a quantity over the hours with enough qualifying data."""
    attribute, coverage_attribute = QUANTITY_FIELDS[quantity]
    series: Dict[datetime, float] = {}
    for bucket in buckets:
        value = getattr(bucket, attribute)
        coverage = getattr(bucket, coverage_attribute)
        if value is None or coverage is None or coverage < MIN_HOUR_COVERAGE:
            continue
        series[ensure_utc(bucket.hour)] = float(value)
    return series


def utc_hour(value: str | datetime) -> datetime:
    """Parse an ISO hour key (as stored in snapshots) to an aware UTC datetime."""
    if isinstance(value, datetime):
        return floor_hour(value)
    return floor_hour(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc))
