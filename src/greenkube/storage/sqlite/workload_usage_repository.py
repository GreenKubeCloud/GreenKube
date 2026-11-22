"""SQLite workload usage rollup repository (roadmap E3, BUG-12)."""

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
from greenkube.utils.time import ensure_utc, to_iso_z

_HOUR = "strftime('%Y-%m-%dT%H:00:00Z', \"timestamp\")"

#: Page cache (KiB) used while a scan aggregates the rollup; it also bounds each sorter's memory.
_SCAN_CACHE_KIB = 2048


def _ph(_index: int) -> str:
    return "?"


def _encode(value: datetime) -> str:
    return to_iso_z(value)


def _decode(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    return ensure_utc(value)


#: Pods per ``IN (...)`` lookup of the last restart counts.
POD_CHUNK = 500


def _pod_chunks(pods: Sequence[Tuple[str, str]]) -> List[Tuple[str, List[str]]]:
    """``(namespace, pod names)`` groups of at most :data:`POD_CHUNK` pods."""
    by_namespace: Dict[str, List[str]] = {}
    for namespace, pod_name in pods:
        by_namespace.setdefault(namespace, []).append(pod_name)
    return [
        (namespace, names[index : index + POD_CHUNK])
        for namespace, names in sorted(by_namespace.items())
        for index in range(0, len(names), POD_CHUNK)
    ]


def _count(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _params(start: datetime, end: datetime, namespace: Optional[str]) -> Tuple[Any, ...]:
    values: Tuple[Any, ...] = (_encode(start), _encode(end))
    return (*values, namespace) if namespace is not None else values


def _input_rows(rows: Sequence[Any], columns: Tuple[Tuple[str, str], ...]) -> List[UsageInputRow]:
    result: List[UsageInputRow] = []
    names = [name for _, name in columns]
    for row in rows:
        values: Dict[str, Any] = dict(zip(names, tuple(row)))
        values["timestamp"] = _decode(values.get("timestamp"))
        if values.get("sample_count") is None:
            values["sample_count"] = 1
        result.append(UsageInputRow(**values))
    return result


class SQLiteWorkloadUsageRepository(WorkloadUsageRepository):
    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def _fetch(self, sql: str, params: Sequence[Any]) -> List[Any]:
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(sql, tuple(params)) as cursor:
                return list(await cursor.fetchall())

    async def _fetch_bounded(self, queries: Sequence[Tuple[str, Sequence[Any]]]) -> List[List[Any]]:
        """Run scan aggregations with a small page cache, no memory map and on-disk sorts.

        SQLite gives every sorter of a query up to ``cache_size`` of memory and
        maps up to ``mmap_size`` of the database file into the process; the
        connection defaults (64 MiB, 256 MiB) would put the scan's peak RSS far
        above the rows it returns. The settings are restored afterwards.
        """
        async with self.db_manager.connection_scope() as conn:
            saved = {}
            for pragma in ("cache_size", "mmap_size", "temp_store"):
                async with conn.execute(f"PRAGMA {pragma}") as cursor:
                    row = await cursor.fetchone()
                saved[pragma] = row[0] if row else None
            await conn.execute(f"PRAGMA cache_size=-{_SCAN_CACHE_KIB}")
            await conn.execute("PRAGMA mmap_size=0")
            await conn.execute("PRAGMA temp_store=FILE")
            try:
                results = []
                for sql, params in queries:
                    async with conn.execute(sql, tuple(params)) as cursor:
                        results.append(list(await cursor.fetchall()))
                return results
            finally:
                for pragma, value in saved.items():
                    if value is not None:
                        await conn.execute(f"PRAGMA {pragma}={int(value)}")

    # ------------------------------------------------------------------ #
    # Rollup maintenance                                                  #
    # ------------------------------------------------------------------ #

    async def _raw_rows(self, table: str, columns: Tuple[Tuple[str, str], ...], hour: datetime) -> List[UsageInputRow]:
        # The loose timestamp range keeps the index usable; the bucket predicate
        # is exact (fractional seconds sort before 'Z').
        sql = (
            f"SELECT {q.select_list(columns, chr(34) + 'timestamp' + chr(34))} FROM {table} "
            f'WHERE "timestamp" >= ? AND "timestamp" < ? AND {_HOUR} = ?'
        )
        rows = await self._fetch(
            sql,
            (
                _encode(hour - timedelta(seconds=1)),
                _encode(hour + timedelta(hours=1, seconds=1)),
                _encode(hour),
            ),
        )
        return _input_rows(rows, columns)

    async def _hourly_rows(
        self, table: str, columns: Tuple[Tuple[str, str], ...], hour: datetime
    ) -> List[UsageInputRow]:
        sql = f"SELECT {q.select_list(columns, 'hour_bucket')} FROM {table} WHERE hour_bucket = ?"
        return _input_rows(await self._fetch(sql, (_encode(hour),)), columns)

    async def read_restart_context(self, hour: datetime, seconds: float) -> List[UsageInputRow]:
        hour = ensure_utc(hour)
        rows = await self._fetch(
            'SELECT namespace, pod_name, "timestamp", restart_count FROM combined_metrics '
            'WHERE "timestamp" >= ? AND "timestamp" < ?',
            (_encode(hour - timedelta(seconds=seconds)), _encode(hour)),
        )
        return [
            UsageInputRow(namespace=row[0], pod_name=row[1], timestamp=_decode(row[2]), restart_count=_count(row[3]))
            for row in rows
        ]

    async def read_last_restart_counts(self, hour: datetime, pods: Sequence[Tuple[str, str]]) -> List[UsageInputRow]:
        bound = _encode(ensure_utc(hour))
        latest: Dict[Tuple[str, str], Tuple[datetime, int]] = {}
        for namespace, names in _pod_chunks(pods):
            marks = ", ".join("?" for _ in names)
            # SQLite returns the bare column of the row holding MAX(): the count at the newest sample.
            for table, column in (("combined_metrics", '"timestamp"'), ("combined_metrics_hourly", "hour_bucket")):
                rows = await self._fetch(
                    f"SELECT pod_name, MAX({column}), restart_count FROM {table} "
                    f"WHERE namespace = ? AND pod_name IN ({marks}) AND {column} < ? AND restart_count IS NOT NULL "
                    "GROUP BY pod_name",
                    (namespace, *names, bound),
                )
                for pod_name, stamp, count in rows:
                    timestamp, value = _decode(stamp), _count(count)
                    key = (namespace, pod_name)
                    if timestamp is None or value is None:
                        continue
                    if key not in latest or timestamp > latest[key][0]:
                        latest[key] = (timestamp, value)
        return [
            UsageInputRow(namespace=namespace, pod_name=pod_name, timestamp=timestamp, restart_count=count)
            for (namespace, pod_name), (timestamp, count) in latest.items()
        ]

    async def legacy_restart_hours(self, start: datetime, end: datetime) -> List[datetime]:
        rows = await self._fetch(
            "SELECT DISTINCT hour_bucket FROM workload_usage_hourly WHERE hour_bucket >= ? AND hour_bucket < ? "
            "AND restart_count IS NOT NULL AND restart_increase IS NULL",
            (_encode(ensure_utc(start)), _encode(ensure_utc(end))),
        )
        return [value for value in (_decode(row[0]) for row in rows) if value is not None]

    async def read_hourly_restart_context(self, hour: datetime) -> List[UsageInputRow]:
        previous = ensure_utc(hour) - timedelta(hours=1)
        rows = await self._fetch(
            "SELECT namespace, pod_name, hour_bucket, restart_count FROM combined_metrics_hourly WHERE hour_bucket = ?",
            (_encode(previous),),
        )
        return [
            UsageInputRow(namespace=row[0], pod_name=row[1], timestamp=_decode(row[2]), restart_count=_count(row[3]))
            for row in rows
        ]

    async def read_hour_inputs(self, hour: datetime) -> Tuple[List[UsageInputRow], List[UsageInputRow], str]:
        from greenkube.storage.raw_hourly_split import raw_hour_is_complete

        hour = ensure_utc(hour)
        sources = set()
        # Raw rows of an hour already pruned are only the late ones: never let them
        # replace the complete hourly aggregate (ROL-02).
        pods = []
        if await raw_hour_is_complete(self.db_manager, "combined_metrics", hour):
            pods = await self._raw_rows("combined_metrics", q.RAW_POD_COLUMNS, hour)
        if pods:
            sources.add("raw")
        else:
            pods = await self._hourly_rows("combined_metrics_hourly", q.HOURLY_POD_COLUMNS, hour)
            if pods:
                sources.add("hourly")
        containers = []
        if await raw_hour_is_complete(self.db_manager, "container_metrics", hour):
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
        bucket = _encode(ensure_utc(hour))
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
                "UPDATE workload_usage_hourly SET restart_increase = ? WHERE hour_bucket = ? AND namespace = ? "
                "AND target_kind = ? AND target_name = ? AND container_name = ? AND node = ?",
                values,
            )

    async def replace_hour(self, hour: datetime, rows: Sequence[WorkloadUsageHour]) -> None:
        sql = q.upsert_sql(["?"] * len(ROLLUP_COLUMNS))
        bucket = _encode(ensure_utc(hour))
        values = []
        for row in rows:
            item = list(rollup_values(row))
            item[0] = bucket
            values.append(tuple(item))
        async with self.db_manager.transaction() as conn:
            await conn.execute("DELETE FROM workload_usage_hourly WHERE hour_bucket = ?", (bucket,))
            if values:
                await conn.executemany(sql, values)

    async def earliest_input_time(self) -> Optional[datetime]:
        rows = await self._fetch(
            'SELECT (SELECT MIN("timestamp") FROM combined_metrics), '
            "(SELECT MIN(hour_bucket) FROM combined_metrics_hourly)",
            (),
        )
        values = [_decode(value) for value in tuple(rows[0])] if rows else []
        present = [value for value in values if value is not None]
        return min(present) if present else None

    async def latest_rolled_hour(self) -> Optional[datetime]:
        rows = await self._fetch("SELECT MAX(hour_bucket) FROM workload_usage_hourly", ())
        return _decode(rows[0][0]) if rows and rows[0][0] else None

    async def input_high_water_marks(self) -> Dict[str, int]:
        selects = ", ".join(f"(SELECT MAX(id) FROM {table})" for table in INPUT_TABLES)
        rows = await self._fetch(f"SELECT {selects}", ())
        values = tuple(rows[0]) if rows else (None,) * len(INPUT_TABLES)
        return {table: int(value or 0) for table, value in zip(INPUT_TABLES, values)}

    async def read_rollup_marks(self) -> Dict[str, int]:
        rows = await self._fetch("SELECT input_table, last_id FROM workload_usage_rollup_marks", ())
        return {str(row[0]): int(row[1]) for row in rows}

    async def save_rollup_marks(self, marks: Mapping[str, int]) -> None:
        now = _encode(datetime.now(timezone.utc))
        async with self.db_manager.transaction() as conn:
            await conn.executemany(
                "INSERT INTO workload_usage_rollup_marks (input_table, last_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(input_table) DO UPDATE SET last_id = excluded.last_id, updated_at = excluded.updated_at",
                [(table, int(value), now) for table, value in marks.items()],
            )

    async def hours_written_between(self, after: Mapping[str, int], upto: Mapping[str, int]) -> List[datetime]:
        hours: set = set()
        for table, lower, upper in q.mark_ranges(after, upto):
            rows = await self._fetch(
                f"SELECT DISTINCT {_HOUR} FROM {table} WHERE id > ? AND id <= ?",
                (lower, upper),
            )
            hours.update(value for value in (_decode(row[0]) for row in rows) if value is not None)
        return sorted(hours)

    async def prune_before(self, cutoff: datetime) -> int:
        async with self.db_manager.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM workload_usage_hourly WHERE hour_bucket < ?", (_encode(ensure_utc(cutoff)),)
            )
            return max(cursor.rowcount, 0)

    async def read_hours(
        self, start: datetime, end: datetime, namespace: Optional[str] = None
    ) -> List[WorkloadUsageHour]:
        where = "hour_bucket >= ? AND hour_bucket < ?" + (" AND namespace = ?" if namespace is not None else "")
        rows = await self._fetch(
            f"SELECT {', '.join(ROLLUP_COLUMNS)} FROM workload_usage_hourly WHERE {where} "
            "ORDER BY hour_bucket, namespace, target_kind, target_name, container_name, node",
            _params(start, end, namespace),
        )
        return [q.hour_from_values(dict(zip(ROLLUP_COLUMNS, tuple(row))), _decode) for row in rows]

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
        params = q.scope_params(_encode(start), _encode(end), namespace, scope)
        summaries, collection = await self._fetch_bounded(
            [
                (q.summary_sql(_ph, namespace, scope), params),
                # IMP-04: collection is cluster-wide (all live namespaces), even for a namespace-scoped read.
                (q.collection_sql(_ph, None, scope), q.scope_params(_encode(start), _encode(end), None, scope)),
            ]
        )
        return q.build_snapshot(summaries, _decode, collection)

    async def read_baseline_rows(
        self,
        start: datetime,
        end: datetime,
        namespace: Optional[str] = None,
        snapshot: Optional[UsageSnapshot] = None,
        namespaces: Optional[Collection[str]] = None,
    ) -> Tuple[List[BaselineMetric], List[BaselineMetric]]:
        scope = sorted(namespaces) if namespaces is not None else None
        params = q.scope_params(_encode(start), _encode(end), namespace, scope)
        (rows,) = await self._fetch_bounded([(q.baseline_sql(_ph, namespace, scope), params)])
        summaries = snapshot.summaries if snapshot is not None else {}
        return q.split_baseline([q.baseline_from_row(row, _decode, summaries) for row in rows])
