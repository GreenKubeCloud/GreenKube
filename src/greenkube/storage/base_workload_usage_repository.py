"""Repository contract for the ``workload_usage_hourly`` rollup (roadmap E3, BUG-12)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from greenkube.models.workload_usage import BaselineMetric, UsageSnapshot, WorkloadUsageHour

#: Columns of ``workload_usage_hourly`` in insertion order (identity first).
ROLLUP_COLUMNS: Tuple[str, ...] = (
    "hour_bucket",
    "namespace",
    "target_kind",
    "target_name",
    "container_name",
    "node",
    "hour_of_day",
    "sample_count",
    "pod_count",
    "step_count",
    "duration_seconds",
    "cpu_sample_count",
    "cpu_usage_avg",
    "cpu_usage_sq_avg",
    "cpu_usage_p50",
    "cpu_usage_p90",
    "cpu_usage_p95",
    "cpu_usage_p99",
    "cpu_usage_max",
    "memory_sample_count",
    "memory_usage_avg",
    "memory_usage_p50",
    "memory_usage_p90",
    "memory_usage_p95",
    "memory_usage_p99",
    "memory_usage_max",
    "cpu_request",
    "memory_request",
    "cpu_request_sum",
    "memory_request_sum",
    "cpu_limit",
    "memory_limit",
    "restart_count",
    "total_cost",
    "cost_cpu",
    "cost_ram",
    "cost_storage",
    "cost_network",
    "cost_lb",
    "cost_source",
    "co2e_grams",
    "embodied_co2e_grams",
    "joules",
    "pue",
    "grid_intensity",
    "intensity_sample_count",
    "emaps_zone",
    "node_instance_type",
    "source",
)


@dataclass(slots=True)
class UsageInputRow:
    """The columns of one raw or hourly metric row the rollup reads."""

    namespace: str
    pod_name: str
    timestamp: Optional[datetime]
    container_name: Optional[str] = None
    owner_kind: Optional[str] = None
    owner_name: Optional[str] = None
    node: Optional[str] = None
    node_instance_type: Optional[str] = None
    sample_count: int = 1
    duration_seconds: Optional[float] = None
    cpu_usage_millicores: Optional[float] = None
    cpu_usage_max_millicores: Optional[float] = None
    memory_usage_bytes: Optional[float] = None
    memory_usage_max_bytes: Optional[float] = None
    cpu_request: Optional[float] = None
    memory_request: Optional[float] = None
    cpu_limit: Optional[float] = None
    memory_limit: Optional[float] = None
    restart_count: Optional[int] = None
    total_cost: Optional[float] = None
    cost_cpu: Optional[float] = None
    cost_ram: Optional[float] = None
    cost_storage: Optional[float] = None
    cost_network: Optional[float] = None
    cost_lb: Optional[float] = None
    cost_source: Optional[str] = None
    co2e_grams: Optional[float] = None
    embodied_co2e_grams: Optional[float] = None
    joules: Optional[float] = None
    pue: Optional[float] = None
    grid_intensity: Optional[float] = None
    emaps_zone: Optional[str] = None


def rollup_values(row: WorkloadUsageHour) -> Tuple[Any, ...]:
    """Values of a rollup row in :data:`ROLLUP_COLUMNS` order."""
    return tuple(getattr(row, column) for column in ROLLUP_COLUMNS)


class WorkloadUsageRepository(ABC):
    """Reads metric rows for the rollup, stores rollup rows and summarizes them in SQL."""

    # ------------------------------------------------------------------ #
    # Rollup maintenance                                                  #
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def read_hour_inputs(self, hour: datetime) -> Tuple[List[UsageInputRow], List[UsageInputRow], str]:
        """Return ``(pod_rows, container_rows, source)`` of one hour.

        Each table is read from its raw rows when it still holds the hour,
        otherwise from its compressed hourly table. ``source`` is ``raw``,
        ``hourly`` or ``mixed``.
        """

    @abstractmethod
    async def replace_hour(self, hour: datetime, rows: Sequence[WorkloadUsageHour]) -> None:
        """Atomically replace every rollup row of ``hour`` with ``rows``."""

    @abstractmethod
    async def earliest_input_time(self) -> Optional[datetime]:
        """Return the oldest timestamp of the raw or hourly pod metrics, or None when there are none."""

    @abstractmethod
    async def latest_rolled_hour(self) -> Optional[datetime]:
        """Return the most recent rolled hour, or None when the rollup is empty."""

    @abstractmethod
    async def prune_before(self, cutoff: datetime) -> int:
        """Delete rollup rows of hours before ``cutoff``; return the count."""

    @abstractmethod
    async def read_hours(
        self, start: datetime, end: datetime, namespace: Optional[str] = None
    ) -> List[WorkloadUsageHour]:
        """Return the rollup rows of ``[start, end)`` (tests and diagnostics; not used by scans)."""

    # ------------------------------------------------------------------ #
    # Scan reads (bounded by the number of targets, not by samples)       #
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def read_usage_snapshot(
        self, start: datetime, end: datetime, namespace: Optional[str] = None
    ) -> UsageSnapshot:
        """Summaries per target container, hour-of-day profiles and node summaries over ``[start, end)``."""

    @abstractmethod
    async def read_baseline_rows(
        self,
        start: datetime,
        end: datetime,
        namespace: Optional[str] = None,
        snapshot: Optional[UsageSnapshot] = None,
    ) -> Tuple[List[BaselineMetric], List[BaselineMetric]]:
        """Return ``(pod_rows, container_rows)`` aggregated for impact projections and evidence.

        Pod rows are grouped by target, node, UTC hour of day and requests;
        container rows by target, container, node and requests. With
        ``snapshot``, each row carries its target's usage percentiles.
        """


def create_workload_usage_repository(db_manager: Any, db_type: str) -> WorkloadUsageRepository:
    """Build the rollup repository matching ``DB_TYPE``."""
    if db_type == "postgres":
        from .postgres.workload_usage_repository import PostgresWorkloadUsageRepository

        return PostgresWorkloadUsageRepository(db_manager)
    if db_type == "sqlite":
        from .sqlite.workload_usage_repository import SQLiteWorkloadUsageRepository

        return SQLiteWorkloadUsageRepository(db_manager)
    raise NotImplementedError(f"WorkloadUsageRepository for DB_TYPE '{db_type}' not implemented.")
