"""Upsert guards shared by the SQLite and PostgreSQL metric writers (STO-01).

A window can be collected twice (a forced ``--last`` run, a crash between the
rows and the watermark, a lost watermark). The second pass sees the cluster as
it is later: OpenCost may be down, so its rows carry a modelled or no cost.
These guards keep the stored value of a dimension when the incoming row's
provenance for that dimension is strictly worse.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from ..models.provenance import cost_source_rank_sql
from ..utils.time import ensure_utc

#: Cost columns of the pod and container metric tables, replaced or kept together with ``cost_source``.
COST_COLUMNS: tuple[str, ...] = (
    "total_cost",
    "cost_cpu",
    "cost_ram",
    "cost_storage",
    "cost_network",
    "cost_lb",
    "cost_source",
)
#: Cost columns of ``node_idle_metrics``.
IDLE_COST_COLUMNS: tuple[str, ...] = ("total_cost", "cost_cpu", "cost_ram", "cost_source")


def produced_pod_keys(pod_rows: Iterable[Any]) -> set[tuple[str, str, datetime]]:
    """``(namespace, pod_name, timestamp)`` of the pod rows of a collection pass, timestamps in UTC."""
    return {(row.namespace, row.pod_name, ensure_utc(row.timestamp)) for row in pod_rows if row.timestamp is not None}


def produced_idle_keys(idle_rows: Iterable[Any]) -> set[tuple[str, datetime]]:
    """``(node, timestamp)`` of the ``__idle__`` rows of a collection pass, timestamps in UTC."""
    return {(row.node, ensure_utc(row.timestamp)) for row in idle_rows if row.timestamp is not None}


def cost_guard_assignments(table: str, columns: tuple[str, ...] = COST_COLUMNS, excluded: str = "EXCLUDED") -> str:
    """``SET`` assignments that keep the stored cost columns when the incoming cost source ranks lower."""
    keep_stored = f"{cost_source_rank_sql(f'{excluded}.cost_source')} < {cost_source_rank_sql(f'{table}.cost_source')}"
    return ",\n".join(
        f"{column} = CASE WHEN {keep_stored} THEN {table}.{column} ELSE {excluded}.{column} END" for column in columns
    )
