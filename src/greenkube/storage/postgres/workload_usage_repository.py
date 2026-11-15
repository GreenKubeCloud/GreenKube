"""PostgreSQL workload usage rollup repository (roadmap E3, BUG-12)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from greenkube.models.workload_usage import BaselineMetric, UsageSnapshot, WorkloadUsageHour
from greenkube.storage import workload_usage_sql as q
from greenkube.storage.base_workload_usage_repository import (
    INPUT_TABLES,
    ROLLUP_COLUMNS,
    UsageInputRow,
    WorkloadUsageRepository,
    rollup_values,
)
from greenkube.utils.time import ensure_utc

# UTC hour of a raw row, independent of the session time zone.
_HOUR = "(date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


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

    async def read_restart_context(self, hour: datetime, seconds: float) -> List[UsageInputRow]:
        hour = ensure_utc(hour)
        rows = await self._fetch(
            "SELECT namespace, pod_name, timestamp, restart_count FROM combined_metrics "
            "WHERE timestamp >= $1 AND timestamp < $2",
            (hour - timedelta(seconds=seconds), hour),
        )
        return [
            UsageInputRow(
                namespace=row["namespace"],
                pod_name=row["pod_name"],
                timestamp=_decode(row["timestamp"]),
                restart_count=None if row["restart_count"] is None else int(row["restart_count"]),
            )
            for row in rows
        ]

    async def read_last_restart_counts(self, hour: datetime, pods: Sequence[Tuple[str, str]]) -> List[UsageInputRow]:
        from greenkube.storage.sqlite.workload_usage_repository import _pod_chunks

        bound = ensure_utc(hour)
        latest: Dict[Tuple[str, str], Tuple[datetime, int]] = {}
        for namespace, names in _pod_chunks(pods):
            for table, column in (("combined_metrics", "timestamp"), ("combined_metrics_hourly", "hour_bucket")):
                rows = await self._fetch(
                    f"SELECT DISTINCT ON (pod_name) pod_name, {column} AS ts, restart_count FROM {table} "
                    f"WHERE namespace = $1 AND pod_name = ANY($2::text[]) AND {column} < $3 "
                    f"AND restart_count IS NOT NULL ORDER BY pod_name, {column} DESC",
                    (namespace, list(names), bound),
                )
                for row in rows:
                    timestamp = _decode(row["ts"])
                    key = (namespace, row["pod_name"])
                    if timestamp is None or row["restart_count"] is None:
                        continue
                    if key not in latest or timestamp > latest[key][0]:
                        latest[key] = (timestamp, int(row["restart_count"]))
        return [
            UsageInputRow(namespace=namespace, pod_name=pod_name, timestamp=timestamp, restart_count=count)
            for (namespace, pod_name), (timestamp, count) in latest.items()
        ]

    async def legacy_restart_hours(self, start: datetime, end: datetime) -> List[datetime]:
        rows = await self._fetch(
            "SELECT DISTINCT hour_bucket FROM workload_usage_hourly WHERE hour_bucket >= $1 AND hour_bucket < $2 "
            "AND restart_count IS NOT NULL AND restart_increase IS NULL",
            (ensure_utc(start), ensure_utc(end)),
        )
        return [value for value in (_decode(row["hour_bucket"]) for row in rows) if value is not None]

    async def read_hourly_restart_context(self, hour: datetime) -> List[UsageInputRow]:
        previous = ensure_utc(hour) - timedelta(hours=1)
        rows = await self._fetch(
            "SELECT namespace, pod_name, hour_bucket, restart_count FROM combined_metrics_hourly "
            "WHERE hour_bucket = $1",
            (previous,),
        )
        return [
            UsageInputRow(
                namespace=row["namespace"],
                pod_name=row["pod_name"],
                timestamp=_decode(row["hour_bucket"]),
                restart_count=None if row["restart_count"] is None else int(row["restart_count"]),
            )
            for row in rows
        ]

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

    async def set_restart_increase(self, hour: datetime, rows: Sequence[WorkloadUsageHour]) -> None:
        bucket = ensure_utc(hour)
        values = [
            (
                row.restart_increase,
                bucket,
                row.namespace,
                row.target_kind,
                row.target_name,
                row.container_name,
                row.node,
            )
            for row in rows
            if row.restart_increase is not None
        ]
        if not values:
            return
        async with self.db_manager.transaction() as conn:
            await conn.executemany(
                "UPDATE workload_usage_hourly SET restart_increase = $1 WHERE hour_bucket = $2 AND namespace = $3 "
                "AND target_kind = $4 AND target_name = $5 AND container_name = $6 AND node = $7",
                values,
            )

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

    async def input_high_water_marks(self) -> Dict[str, int]:
        selects = ", ".join(f"(SELECT MAX(id) FROM {table}) AS {table}" for table in INPUT_TABLES)
        rows = await self._fetch(f"SELECT {selects}", ())
        return {table: int((rows[0][table] if rows else None) or 0) for table in INPUT_TABLES}

    async def read_rollup_marks(self) -> Dict[str, int]:
        rows = await self._fetch("SELECT input_table, last_id FROM workload_usage_rollup_marks", ())
        return {str(row["input_table"]): int(row["last_id"]) for row in rows}

    async def save_rollup_marks(self, marks: Mapping[str, int]) -> None:
        now = datetime.now(timezone.utc)
        async with self.db_manager.transaction() as conn:
            await conn.executemany(
                "INSERT INTO workload_usage_rollup_marks (input_table, last_id, updated_at) VALUES ($1, $2, $3) "
                "ON CONFLICT (input_table) DO UPDATE SET last_id = excluded.last_id, updated_at = excluded.updated_at",
                [(table, int(value), now) for table, value in marks.items()],
            )

    async def hours_written_between(self, after: Mapping[str, int], upto: Mapping[str, int]) -> List[datetime]:
        hours: set = set()
        for table, lower, upper in q.mark_ranges(after, upto):
            rows = await self._fetch(
                f"SELECT DISTINCT {_HOUR} AS hour FROM {table} WHERE id > $1 AND id <= $2",
                (lower, upper),
            )
            hours.update(value for value in (_decode(row["hour"]) for row in rows) if value is not None)
        return sorted(hours)

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
        self,
        start: datetime,
        end: datetime,
        namespace: Optional[str] = None,
        namespaces: Optional[Collection[str]] = None,
    ) -> UsageSnapshot:
        scope = sorted(namespaces) if namespaces is not None else None
        params = q.scope_params(ensure_utc(start), ensure_utc(end), namespace, scope)
        summaries = await self._fetch(q.summary_sql(_ph, namespace, scope), params)
        profiles = await self._fetch(q.hour_profile_sql(_ph, namespace, scope), params)
        nodes = await self._fetch(q.node_sql(_ph, namespace, scope), params)
        return q.build_snapshot(summaries, profiles, nodes, _decode)

    async def read_baseline_rows(
        self,
        start: datetime,
        end: datetime,
        namespace: Optional[str] = None,
        snapshot: Optional[UsageSnapshot] = None,
        namespaces: Optional[Collection[str]] = None,
    ) -> Tuple[List[BaselineMetric], List[BaselineMetric]]:
        scope = sorted(namespaces) if namespaces is not None else None
        params = q.scope_params(ensure_utc(start), ensure_utc(end), namespace, scope)
        rows = await self._fetch(q.baseline_sql(_ph, namespace, scope), params)
        summaries = snapshot.summaries if snapshot is not None else {}
        return q.split_baseline([q.baseline_from_row(row, _decode, summaries) for row in rows])
