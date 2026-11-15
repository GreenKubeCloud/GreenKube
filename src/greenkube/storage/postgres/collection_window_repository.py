"""PostgreSQL collection window repository (COL-04)."""

from __future__ import annotations

from datetime import datetime

from greenkube.storage.base_collection_window_repository import (
    STATUS_FAILED,
    STATUS_GAP,
    CollectionWindowRecord,
    CollectionWindowRepository,
)
from greenkube.utils.time import ensure_utc

_COLUMNS = (
    "cluster_uid, source, window_start, window_end, status, expected_steps, steps_with_data, pod_rows, "
    "cpu_series, pods_without_cpu, reason, opencost_total_cost, attributed_cost, collected_at"
)


def _row(row) -> CollectionWindowRecord:
    return CollectionWindowRecord(
        cluster_uid=row["cluster_uid"],
        source=row["source"],
        window_start=ensure_utc(row["window_start"]),
        window_end=ensure_utc(row["window_end"]),
        status=row["status"],
        expected_steps=int(row["expected_steps"]),
        steps_with_data=int(row["steps_with_data"]),
        pod_rows=int(row["pod_rows"]),
        cpu_series=int(row["cpu_series"]),
        pods_without_cpu=int(row["pods_without_cpu"]),
        reason=row["reason"],
        opencost_total_cost=row["opencost_total_cost"],
        attributed_cost=row["attributed_cost"],
        collected_at=ensure_utc(row["collected_at"]),
    )


class PostgresCollectionWindowRepository(CollectionWindowRepository):
    """Collection window records in PostgreSQL."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def record(self, records: list[CollectionWindowRecord]) -> int:
        """Upsert records keyed by ``(cluster_uid, source, window_start)``."""
        if not records:
            return 0
        params = [
            (
                r.cluster_uid,
                r.source,
                r.window_start,
                r.window_end,
                r.status,
                r.expected_steps,
                r.steps_with_data,
                r.pod_rows,
                r.cpu_series,
                r.pods_without_cpu,
                r.reason,
                r.opencost_total_cost,
                r.attributed_cost,
                ensure_utc(r.collected_at) if r.collected_at else None,
            )
            for r in records
        ]
        async with self.db_manager.connection_scope() as conn:
            await conn.executemany(
                f"""INSERT INTO collection_windows ({_COLUMNS})
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, COALESCE($14, NOW()))
                ON CONFLICT (cluster_uid, source, window_start) DO UPDATE SET
                    window_end = EXCLUDED.window_end,
                    status = EXCLUDED.status,
                    expected_steps = EXCLUDED.expected_steps,
                    steps_with_data = EXCLUDED.steps_with_data,
                    pod_rows = EXCLUDED.pod_rows,
                    cpu_series = EXCLUDED.cpu_series,
                    pods_without_cpu = EXCLUDED.pods_without_cpu,
                    reason = EXCLUDED.reason,
                    opencost_total_cost = EXCLUDED.opencost_total_cost,
                    attributed_cost = EXCLUDED.attributed_cost,
                    collected_at = EXCLUDED.collected_at
                -- A re-collection without data (beyond retention) never downgrades stored rows.
                WHERE NOT (collection_windows.status = 'collected' AND EXCLUDED.status = 'failed')""",
                params,
            )
        return len(params)

    async def list_overlapping(
        self, cluster_uid: str, source: str, start: datetime, end: datetime
    ) -> list[CollectionWindowRecord]:
        """Records overlapping ``[start, end)``, oldest first."""
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM collection_windows
                WHERE cluster_uid = $1 AND source = $2 AND window_start < $3 AND window_end > $4
                ORDER BY window_start""",
                cluster_uid,
                source,
                ensure_utc(end),
                ensure_utc(start),
            )
        return [_row(row) for row in rows]

    async def list_failed(
        self, cluster_uid: str, source: str, since: datetime, limit: int
    ) -> list[CollectionWindowRecord]:
        """Failed steps since ``since``, least recently attempted first."""
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM collection_windows
                WHERE cluster_uid = $1 AND source = $2 AND status = $3 AND window_start >= $4
                ORDER BY collected_at, window_start LIMIT $5""",
                cluster_uid,
                source,
                STATUS_FAILED,
                ensure_utc(since),
                max(int(limit), 0),
            )
        return [_row(row) for row in rows]

    async def expire_failed(self, cluster_uid: str, source: str, before: datetime) -> int:
        """Failed steps older than ``before`` become gaps."""
        async with self.db_manager.connection_scope() as conn:
            result = await conn.execute(
                """UPDATE collection_windows SET status = $1
                WHERE cluster_uid = $2 AND source = $3 AND status = $4 AND window_start < $5""",
                STATUS_GAP,
                cluster_uid,
                source,
                STATUS_FAILED,
                ensure_utc(before),
            )
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    async def last_hole(self, cluster_uid: str, source: str) -> CollectionWindowRecord | None:
        """Most recent gap or failed record."""
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                f"""SELECT {_COLUMNS} FROM collection_windows
                WHERE cluster_uid = $1 AND source = $2 AND status IN ($3, $4)
                ORDER BY window_end DESC LIMIT 1""",
                cluster_uid,
                source,
                STATUS_GAP,
                STATUS_FAILED,
            )
        return _row(row) if row else None

    async def has_records(self, cluster_uid: str, source: str) -> bool:
        """True when a record exists for the cluster and source."""
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM collection_windows WHERE cluster_uid = $1 AND source = $2 LIMIT 1",
                cluster_uid,
                source,
            )
        return row is not None

    async def stored_metric_timestamps(self, since: datetime | None = None) -> list[datetime]:
        """Distinct timestamps of the stored pod rows."""
        async with self.db_manager.connection_scope() as conn:
            if since is None:
                rows = await conn.fetch(
                    "SELECT DISTINCT timestamp FROM combined_metrics WHERE timestamp IS NOT NULL ORDER BY timestamp"
                )
            else:
                rows = await conn.fetch(
                    "SELECT DISTINCT timestamp FROM combined_metrics WHERE timestamp >= $1 ORDER BY timestamp",
                    ensure_utc(since),
                )
        return [ensure_utc(row["timestamp"]) for row in rows]
