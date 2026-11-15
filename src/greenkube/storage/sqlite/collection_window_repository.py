"""SQLite collection window repository (COL-04)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from greenkube.storage.base_collection_window_repository import (
    STATUS_FAILED,
    STATUS_GAP,
    CollectionWindowRecord,
    CollectionWindowRepository,
)
from greenkube.utils.time import ensure_utc

# Fixed-width UTC format so that lexicographic order equals chronological order.
_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_COLUMNS = (
    "cluster_uid, source, window_start, window_end, status, expected_steps, steps_with_data, pod_rows, "
    "cpu_series, pods_without_cpu, reason, opencost_total_cost, attributed_cost, collected_at"
)


def _encode(value: datetime) -> str:
    return ensure_utc(value).strftime(_FORMAT)


def _row(row) -> CollectionWindowRecord:
    return CollectionWindowRecord(
        cluster_uid=row[0],
        source=row[1],
        window_start=ensure_utc(row[2]),
        window_end=ensure_utc(row[3]),
        status=row[4],
        expected_steps=int(row[5]),
        steps_with_data=int(row[6]),
        pod_rows=int(row[7]),
        cpu_series=int(row[8]),
        pods_without_cpu=int(row[9]),
        reason=row[10],
        opencost_total_cost=row[11],
        attributed_cost=row[12],
        collected_at=ensure_utc(row[13]),
    )


class SQLiteCollectionWindowRepository(CollectionWindowRepository):
    """Collection window records in SQLite."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def record(self, records: list[CollectionWindowRecord], *, connection: Any = None) -> int:
        """Upsert records keyed by ``(cluster_uid, source, window_start)``."""
        if not records:
            return 0
        now = _encode(datetime.now(timezone.utc))
        params = [
            (
                r.cluster_uid,
                r.source,
                _encode(r.window_start),
                _encode(r.window_end),
                r.status,
                r.expected_steps,
                r.steps_with_data,
                r.pod_rows,
                r.cpu_series,
                r.pods_without_cpu,
                r.reason,
                r.opencost_total_cost,
                r.attributed_cost,
                _encode(r.collected_at) if r.collected_at else now,
            )
            for r in records
        ]
        async with self._transaction(connection) as conn:
            await conn.executemany(
                f"""INSERT INTO collection_windows ({_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cluster_uid, source, window_start) DO UPDATE SET
                    window_end = excluded.window_end,
                    status = excluded.status,
                    expected_steps = excluded.expected_steps,
                    steps_with_data = excluded.steps_with_data,
                    pod_rows = excluded.pod_rows,
                    cpu_series = excluded.cpu_series,
                    pods_without_cpu = excluded.pods_without_cpu,
                    reason = excluded.reason,
                    opencost_total_cost = excluded.opencost_total_cost,
                    attributed_cost = excluded.attributed_cost,
                    collected_at = excluded.collected_at
                -- A re-collection without data (beyond retention) never downgrades stored rows.
                WHERE NOT (collection_windows.status = 'collected' AND excluded.status = 'failed')""",
                params,
            )
        return len(params)

    @asynccontextmanager
    async def _transaction(self, connection: Any = None) -> AsyncIterator[Any]:
        """``connection`` when given (the caller commits), else an own committed transaction."""
        if connection is not None:
            yield connection
            return
        async with self.db_manager.transaction() as conn:
            yield conn

    async def list_overlapping(
        self, cluster_uid: str, source: str, start: datetime, end: datetime
    ) -> list[CollectionWindowRecord]:
        """Records overlapping ``[start, end)``, oldest first."""
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                f"""SELECT {_COLUMNS} FROM collection_windows
                WHERE cluster_uid = ? AND source = ? AND window_start < ? AND window_end > ?
                ORDER BY window_start""",
                (cluster_uid, source, _encode(end), _encode(start)),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row(row) for row in rows]

    async def list_failed(
        self, cluster_uid: str, source: str, since: datetime, limit: int
    ) -> list[CollectionWindowRecord]:
        """Failed steps since ``since``, least recently attempted first."""
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                f"""SELECT {_COLUMNS} FROM collection_windows
                WHERE cluster_uid = ? AND source = ? AND status = ? AND window_start >= ?
                ORDER BY collected_at, window_start LIMIT ?""",
                (cluster_uid, source, STATUS_FAILED, _encode(since), max(int(limit), 0)),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row(row) for row in rows]

    async def expire_failed(self, cluster_uid: str, source: str, before: datetime) -> int:
        """Failed steps older than ``before`` become gaps."""
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                """UPDATE collection_windows SET status = ?
                WHERE cluster_uid = ? AND source = ? AND status = ? AND window_start < ?""",
                (STATUS_GAP, cluster_uid, source, STATUS_FAILED, _encode(before)),
            )
            await conn.commit()
            return int(cursor.rowcount or 0)

    async def last_hole(self, cluster_uid: str, source: str) -> CollectionWindowRecord | None:
        """Most recent gap or failed record."""
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                f"""SELECT {_COLUMNS} FROM collection_windows
                WHERE cluster_uid = ? AND source = ? AND status IN (?, ?)
                ORDER BY window_end DESC LIMIT 1""",
                (cluster_uid, source, STATUS_GAP, STATUS_FAILED),
            ) as cursor:
                row = await cursor.fetchone()
        return _row(row) if row else None

    async def has_records(self, cluster_uid: str, source: str) -> bool:
        """True when a record exists for the cluster and source."""
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                "SELECT 1 FROM collection_windows WHERE cluster_uid = ? AND source = ? LIMIT 1",
                (cluster_uid, source),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def stored_metric_timestamps(self, since: datetime | None = None) -> list[datetime]:
        """Distinct timestamps of the stored pod rows."""
        query = 'SELECT DISTINCT "timestamp" FROM combined_metrics WHERE "timestamp" IS NOT NULL'
        params: tuple[str, ...] = ()
        if since is not None:
            # Stored timestamps are ISO-8601 UTC strings: compare on the fixed-width prefix
            # (one second earlier keeps every format, the exact check below trims it).
            query += ' AND "timestamp" >= ?'
            params = ((ensure_utc(since) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S"),)
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        values = {ensure_utc(row[0]) for row in rows if row[0]}
        if since is not None:
            floor = ensure_utc(since)
            values = {value for value in values if value >= floor}
        return sorted(values)
