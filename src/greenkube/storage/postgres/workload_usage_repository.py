"""PostgreSQL workload usage rollup repository (roadmap E3, BUG-12)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from greenkube.models.workload_usage import BaselineMetric, UsageSnapshot, WorkloadUsageHour
from greenkube.storage import workload_usage_sql as q
from greenkube.storage.base_workload_usage_repository import (
    ROLLUP_COLUMNS,
    UsageInputRow,
    WorkloadUsageRepository,
    rollup_values,
)
from greenkube.utils.time import ensure_utc


def _ph(index: int) -> str:
    return f"${index}"


def _decode(value: Any) -> Optional[datetime]:
    return ensure_utc(value) if value is not None else None


def _params(start: datetime, end: datetime, namespace: Optional[str]) -> Tuple[Any, ...]:
    values: Tuple[Any, ...] = (ensure_utc(start), ensure_utc(end))
    return (*values, namespace) if namespace is not None else values


def _number(value: Any) -> Any:
    # NUMERIC/REAL columns may come back as Decimal; the rollup works in floats.
    if value is None or isinstance(value, (int, float, str, datetime)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _input_rows(rows: Sequence[Any], columns: Tuple[Tuple[str, str], ...]) -> List[UsageInputRow]:
    result: List[UsageInputRow] = []
    names = [name for _, name in columns]
    for row in rows:
        values: Dict[str, Any] = {name: _number(value) for name, value in zip(names, tuple(row))}
        values["timestamp"] = _decode(values.get("timestamp"))
        if values.get("sample_count") is None:
            values["sample_count"] = 1
        else:
            values["sample_count"] = int(values["sample_count"])
        result.append(UsageInputRow(**values))
    return result


class PostgresWorkloadUsageRepository(WorkloadUsageRepository):
    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def _fetch(self, sql: str, params: Sequence[Any]) -> List[Any]:
        async with self.db_manager.connection_scope() as conn:
            return list(await conn.fetch(sql, *params))

    # ------------------------------------------------------------------ #
    # Rollup maintenance                                                  #
    # ------------------------------------------------------------------ #

    async def _raw_rows(self, table: str, columns: Tuple[Tuple[str, str], ...], hour: datetime) -> List[UsageInputRow]:
        sql = f"SELECT {q.select_list(columns, 'timestamp')} FROM {table} WHERE timestamp >= $1 AND timestamp < $2"
        return _input_rows(await self._fetch(sql, (hour, hour + timedelta(hours=1))), columns)

    async def _hourly_rows(
        self, table: str, columns: Tuple[Tuple[str, str], ...], hour: datetime
    ) -> List[UsageInputRow]:
        sql = f"SELECT {q.select_list(columns, 'hour_bucket')} FROM {table} WHERE hour_bucket = $1"
        return _input_rows(await self._fetch(sql, (hour,)), columns)

    async def read_hour_inputs(self, hour: datetime) -> Tuple[List[UsageInputRow], List[UsageInputRow], str]:
        hour = ensure_utc(hour)
        sources = set()
        pods = await self._raw_rows("combined_metrics", q.RAW_POD_COLUMNS, hour)
        if pods:
            sources.add("raw")
        else:
            pods = await self._hourly_rows("combined_metrics_hourly", q.HOURLY_POD_COLUMNS, hour)
            if pods:
                sources.add("hourly")
        containers = await self._raw_rows("container_metrics", q.RAW_CONTAINER_COLUMNS, hour)
        if containers:
            sources.add("raw")
        else:
            containers = await self._hourly_rows("container_metrics_hourly", q.HOURLY_CONTAINER_COLUMNS, hour)
            if containers:
                sources.add("hourly")
        source = sources.pop() if len(sources) == 1 else ("mixed" if sources else "raw")
        return pods, containers, source

    async def replace_hour(self, hour: datetime, rows: Sequence[WorkloadUsageHour]) -> None:
        sql = q.upsert_sql([f"${index}" for index in range(1, len(ROLLUP_COLUMNS) + 1)])
        bucket = ensure_utc(hour)
        values = []
        for row in rows:
            item = list(rollup_values(row))
            item[0] = bucket
            values.append(tuple(item))
        async with self.db_manager.transaction() as conn:
            await conn.execute("DELETE FROM workload_usage_hourly WHERE hour_bucket = $1", bucket)
            if values:
                await conn.executemany(sql, values)

    async def earliest_input_time(self) -> Optional[datetime]:
        rows = await self._fetch(
            "SELECT (SELECT MIN(timestamp) FROM combined_metrics) AS raw_min, "
            "(SELECT MIN(hour_bucket) FROM combined_metrics_hourly) AS hourly_min",
            (),
        )
        values = [_decode(rows[0]["raw_min"]), _decode(rows[0]["hourly_min"])] if rows else []
        present = [value for value in values if value is not None]
        return min(present) if present else None

    async def latest_rolled_hour(self) -> Optional[datetime]:
        rows = await self._fetch("SELECT MAX(hour_bucket) AS latest FROM workload_usage_hourly", ())
        return _decode(rows[0]["latest"]) if rows else None

    async def prune_before(self, cutoff: datetime) -> int:
        async with self.db_manager.transaction() as conn:
            status = await conn.execute("DELETE FROM workload_usage_hourly WHERE hour_bucket < $1", ensure_utc(cutoff))
        try:
            return int(str(status).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def read_hours(
        self, start: datetime, end: datetime, namespace: Optional[str] = None
    ) -> List[WorkloadUsageHour]:
        where = "hour_bucket >= $1 AND hour_bucket < $2" + (" AND namespace = $3" if namespace is not None else "")
        rows = await self._fetch(
            f"SELECT {', '.join(ROLLUP_COLUMNS)} FROM workload_usage_hourly WHERE {where} "
            "ORDER BY hour_bucket, namespace, target_kind, target_name, container_name, node",
            _params(start, end, namespace),
        )
        return [q.hour_from_values(dict(row), _decode) for row in rows]

    # ------------------------------------------------------------------ #
    # Scan reads                                                          #
    # ------------------------------------------------------------------ #

    async def read_usage_snapshot(
        self, start: datetime, end: datetime, namespace: Optional[str] = None
    ) -> UsageSnapshot:
        params = _params(start, end, namespace)
        summaries = await self._fetch(q.summary_sql(_ph, namespace), params)
        profiles = await self._fetch(q.hour_profile_sql(_ph, namespace), params)
        nodes = await self._fetch(q.node_sql(_ph, namespace), params)
        return q.build_snapshot(summaries, profiles, nodes, _decode)

    async def read_baseline_rows(
        self,
        start: datetime,
        end: datetime,
        namespace: Optional[str] = None,
        snapshot: Optional[UsageSnapshot] = None,
    ) -> Tuple[List[BaselineMetric], List[BaselineMetric]]:
        rows = await self._fetch(q.baseline_sql(_ph, namespace), _params(start, end, namespace))
        summaries = snapshot.summaries if snapshot is not None else {}
        return q.split_baseline([q.baseline_from_row(row, _decode, summaries) for row in rows])
