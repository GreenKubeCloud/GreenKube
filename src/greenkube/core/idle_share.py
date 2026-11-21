"""Node idle share next to the allocated totals (NRG-02, WP-05.3).

The node idle share (unreserved capacity, ``__idle__`` rows) belongs to no
namespace. A cluster-wide total of a window is ``allocated + idle`` only when
the stored idle history covers the window: idle rows exist only since the
upgrade that added them, so a window with pod rows before the first idle row
gets an unknown (``None``) idle share and cluster total, never a partial one.
The API, the report, the export, the dashboard summary cache and the
Prometheus gauges share these helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

#: Allocated measure → idle and cluster-total field suffix.
IDLE_MEASURES = {
    "total_co2e_grams": "co2e_grams",
    "total_embodied_co2e_grams": "embodied_co2e_grams",
    "total_cost": "cost",
    "total_energy_joules": "energy_joules",
}


async def idle_history_gap(repo: Any, start: datetime, end: datetime) -> tuple[Optional[datetime], bool]:
    """First instant of the stored idle rows, and whether pod rows of ``[start, end)`` precede it.

    Idle rows exist only since the upgrade that added them (about the raw
    retention before the first compression): a window with pod rows before
    that instant would get an idle share, and a cluster total, that cover only
    part of it. Such a window reports them as unknown (review of NRG-02).
    """
    since = await repo.idle_data_since()
    if since is None or since <= start:
        return since, False
    before = await repo.aggregate_summary(start_time=start, end_time=min(since, end), namespace=None)
    return since, bool(before.get("row_count"))


def with_idle_share(allocated: Any, idle: Optional[dict]) -> dict:
    """Idle share and cluster totals (allocated + idle) of a summary or a bucket; empty when unknown.

    ``allocated`` is a mapping with the ``total_*`` keys of :data:`IDLE_MEASURES`
    (or an object with those attributes); ``idle`` maps ``idle_<suffix>`` to a value.
    """
    if idle is None:
        return {}
    fields: dict = {}
    for total, suffix in IDLE_MEASURES.items():
        value = float(idle.get(f"idle_{suffix}") or 0.0)
        base = allocated.get(total) if isinstance(allocated, dict) else getattr(allocated, total, None)
        fields[f"idle_{suffix}"] = value
        fields[f"cluster_total_{suffix}"] = float(base or 0.0) + value
    return fields


#: Stored idle columns of a ``metrics_summary`` row (migration 0250), after the allocated columns.
SUMMARY_IDLE_COLUMNS = (
    "idle_co2e_grams",
    "idle_embodied_co2e_grams",
    "idle_cost",
    "idle_energy_joules",
    "idle_data_since",
)


def stored_summary_idle_fields(row: Any) -> dict:
    """``MetricsSummaryRow`` idle fields of a stored ``metrics_summary`` row (a mapping).

    The cluster totals are rebuilt from the stored allocated and idle values;
    an unknown idle share (NULL ``idle_energy_joules``) leaves them all ``None``.
    """
    keys = set(row.keys())
    since = row["idle_data_since"] if "idle_data_since" in keys else None
    if isinstance(since, str):
        since = datetime.fromisoformat(since)
    fields: dict = {"idle_data_since": since}
    if "idle_energy_joules" not in keys or row["idle_energy_joules"] is None:
        return fields
    idle = {column: row[column] for column in SUMMARY_IDLE_COLUMNS[:4]}
    return {**fields, **with_idle_share({total: row[total] for total in IDLE_MEASURES}, idle)}


def summary_idle_values(row: Any) -> list:
    """Values of :data:`SUMMARY_IDLE_COLUMNS` to store for a ``MetricsSummaryRow``."""
    known = row.idle_energy_joules is not None
    values = [getattr(row, column) if known else None for column in SUMMARY_IDLE_COLUMNS[:4]]
    return [*values, row.idle_data_since]


async def window_idle_share(repo: Any, start: datetime, end: datetime, allocated: Any) -> dict:
    """``idle_data_since`` plus the idle share and cluster totals of a cluster-wide window, when known."""
    since, short = await idle_history_gap(repo, start, end)
    idle = None if short else await repo.aggregate_idle_summary(start, end)
    return {"idle_data_since": since, **with_idle_share(allocated, idle)}
