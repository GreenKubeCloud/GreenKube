# src/greenkube/core/metrics_compressor.py
"""
Lossless hourly compression of the metrics tables (roadmap A3, BUG-02).

Raw 5-minute pod rows (``combined_metrics``) and container rows
(``container_metrics``) are aggregated into ``*_hourly`` tables, then raw rows
past their retention are pruned. The job is lossless by construction:

* only **closed** hours are compressed: a bucket is eligible when its end is
  ``<= floor_hour(min(now - METRICS_COMPRESSION_AGE_HOURS, collection_watermark))``;
* each table keeps a **compression watermark** (``metrics_compression_watermarks``):
  the exclusive end of the last hour aggregated from its complete raw set.
  Compression starts at the watermark, so already compressed hours are never
  re-read or overwritten by a partial raw set;
* hours are compressed and the watermark advanced in **one transaction**
  (bounded to one day of buckets per transaction);
* raw pruning uses an **hour-aligned** cutoff and never deletes rows at or
  after the compression watermark, so an hour is either fully raw or fully
  deleted — never half pruned.

The same job keeps the ``workload_usage_hourly`` rollup up to date (roadmap
E3, BUG-12): it re-rolls the last day of hours from their raw rows (or their
hourly aggregates once raw rows are pruned) before compression, and prunes it
with the hourly retention. Recommendation scans read summaries of that rollup
instead of raw rows.

The savings ledger has its own compression (owned by the ledger, see C0.6).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..core.config import Config, get_config
from ..utils.date_utils import to_iso_z

logger = logging.getLogger(__name__)

#: Upper bound of hours aggregated in one transaction.
MAX_HOURS_PER_TRANSACTION = 24
#: Raw rows are refreshed by the carbon-intensity recompute for this many hours
#: (``CARBON_RECOMPUTE_WINDOW_HOURS`` in ``cli/start.py``). Compressed hours are
#: never re-read, so a shorter compression age freezes their hourly carbon
#: figures before the recompute has consolidated them.
CARBON_RECOMPUTE_HORIZON_HOURS = 24

_SQLITE_HOUR = "strftime('%Y-%m-%dT%H:00:00Z', {column})"
_POSTGRES_HOUR = "(date_trunc('hour', {column} AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


def floor_hour(value: datetime) -> datetime:
    """Return the UTC hour that contains ``value``."""
    value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.replace(minute=0, second=0, microsecond=0)


def _source_aggregate(column: str) -> str:
    """Collapse per-row provenance into one hourly value (``mixed`` when sources differ)."""
    return (
        f"CASE WHEN COUNT({column}) = 0 THEN NULL "
        f"WHEN COUNT({column}) = COUNT(*) AND MIN({column}) = MAX({column}) THEN MIN({column}) "
        f"ELSE 'mixed' END"
    )


def _mode(column: str, backend: str) -> str:
    if backend == "postgres":
        return f"MODE() WITHIN GROUP (ORDER BY {column})"
    return column  # SQLite returns a value from the group for bare columns.


@dataclass(frozen=True)
class _MetricsTable:
    raw: str
    hourly: str
    identity: tuple[str, ...]
    pod_scope: bool

    def timestamp_column(self, backend: str) -> str:
        return '"timestamp"' if backend == "sqlite" else "timestamp"

    def hour_expr(self, backend: str) -> str:
        template = _SQLITE_HOUR if backend == "sqlite" else _POSTGRES_HOUR
        return template.format(column=self.timestamp_column(backend))

    def aggregate_columns(self, backend: str) -> list[tuple[str, str]]:
        """Return ``(hourly_column, select_expression)`` pairs."""
        cast_int = "CAST(AVG({c}) AS INTEGER)" if backend == "sqlite" else "AVG({c})::INTEGER"
        cast_big = "CAST(AVG({c}) AS INTEGER)" if backend == "sqlite" else "AVG({c})::BIGINT"
        columns: list[tuple[str, str]] = [(column, column) for column in self.identity]
        columns += [
            ("hour_bucket", self.hour_expr(backend)),
            ("sample_count", "COUNT(*)"),
            ("total_cost", "SUM(total_cost)"),
            ("co2e_grams", "SUM(co2e_grams)"),
            ("embodied_co2e_grams", "SUM(COALESCE(embodied_co2e_grams, 0))"),
            ("pue", "AVG(pue)"),
            ("grid_intensity", "AVG(grid_intensity)"),
            ("joules", "SUM(joules)"),
            ("cpu_request", "MAX(cpu_request)"),
            ("memory_request", "MAX(memory_request)"),
            ("cpu_usage_avg", cast_int.format(c="cpu_usage_millicores")),
            ("cpu_usage_max", "MAX(cpu_usage_millicores)"),
            ("memory_usage_avg", cast_big.format(c="memory_usage_bytes")),
            ("memory_usage_max", "MAX(memory_usage_bytes)"),
            ("owner_kind", _mode("owner_kind", backend)),
            ("owner_name", _mode("owner_name", backend)),
            ("duration_seconds", "SUM(COALESCE(duration_seconds, 0))"),
            (
                "is_estimated",
                "MAX(COALESCE(is_estimated, 0))" if backend == "sqlite" else "BOOL_OR(COALESCE(is_estimated, FALSE))",
            ),
            ("estimation_reasons", "'[]'"),
            ("calculation_version", "MAX(calculation_version)"),
            ("cost_cpu", "SUM(cost_cpu)"),
            ("cost_ram", "SUM(cost_ram)"),
            ("cost_storage", "SUM(cost_storage)"),
            ("cost_network", "SUM(cost_network)"),
            ("cost_lb", "SUM(cost_lb)"),
            ("cost_source", _source_aggregate("cost_source")),
            ("usage_source", _source_aggregate("usage_source")),
            ("power_source", _source_aggregate("power_source")),
            ("intensity_source", _source_aggregate("intensity_source")),
            ("emaps_zone", _mode("emaps_zone", backend)),
        ]
        if self.pod_scope:
            columns += [
                ("network_receive_bytes", "AVG(network_receive_bytes)"),
                ("network_transmit_bytes", "AVG(network_transmit_bytes)"),
                ("disk_read_bytes", "AVG(disk_read_bytes)"),
                ("disk_write_bytes", "AVG(disk_write_bytes)"),
                ("storage_request_bytes", "MAX(storage_request_bytes)"),
                ("storage_usage_bytes", "MAX(storage_usage_bytes)"),
                ("gpu_usage_millicores", "MAX(gpu_usage_millicores)"),
                ("restart_count", "MAX(restart_count)"),
                ("node", _mode("node", backend)),
                ("node_instance_type", _mode("node_instance_type", backend)),
                ("node_zone", _mode("node_zone", backend)),
            ]
        else:
            columns += [
                ("cpu_limit", "MAX(cpu_limit)"),
                ("memory_limit", "MAX(memory_limit)"),
            ]
        return columns

    def compress_sql(self, backend: str) -> str:
        columns = self.aggregate_columns(backend)
        names = ", ".join(name for name, _ in columns)
        selects = ",\n                ".join(f"{expr} AS {name}" for name, expr in columns)
        identity = ", ".join(self.identity)
        hour = self.hour_expr(backend)
        ts = self.timestamp_column(backend)
        updates = ",\n                ".join(
            f"{name} = excluded.{name}" for name, _ in columns if name not in self.identity and name != "hour_bucket"
        )
        conflict = ", ".join((*self.identity, "hour_bucket"))
        if backend == "sqlite":
            # The loose timestamp range keeps the index usable; the bucket
            # predicate is the exact one (fractional seconds sort before 'Z').
            where = f"{ts} >= ? AND {ts} < ? AND {hour} >= ? AND {hour} < ?"
        else:
            where = f"{ts} >= $1 AND {ts} < $2"
        return f"""
            INSERT INTO {self.hourly} ({names})
            SELECT
                {selects}
            FROM {self.raw}
            WHERE {where}
            GROUP BY {identity}, {hour}
            ON CONFLICT ({conflict}) DO UPDATE SET
                {updates}
        """


TABLES: tuple[_MetricsTable, ...] = (
    _MetricsTable("combined_metrics", "combined_metrics_hourly", ("pod_name", "namespace"), pod_scope=True),
    _MetricsTable(
        "container_metrics",
        "container_metrics_hourly",
        ("pod_name", "namespace", "container_name"),
        pod_scope=False,
    ),
)

CollectionWatermarkProvider = Callable[[], Awaitable[Optional[datetime]]]


class MetricsCompressor:
    """Compresses raw metrics into hourly aggregates and manages retention."""

    #: The short-compression-age warning is logged once per process.
    _short_age_warned = False

    def __init__(
        self,
        db_manager,
        config: Config | None = None,
        *,
        collection_watermark_provider: CollectionWatermarkProvider | None = None,
        rollup_enabled: bool = True,
    ):
        """
        Args:
            db_manager: The database manager.
            config: Optional configuration override.
            collection_watermark_provider: Optional coroutine returning the end
                of the last completely collected window. Compression never
                passes it, so hours that are still being (back)filled stay raw.
            rollup_enabled: Refresh and prune the ``workload_usage_hourly``
                rollup in the same cycle (roadmap E3).
        """
        self._db = db_manager
        self._config = config or get_config()
        self._collection_watermark_provider = collection_watermark_provider
        self._rollup_enabled = rollup_enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, now: datetime | None = None) -> dict:
        """Execute the full compression + retention cycle.

        Returns:
            Dict with keys: hours_compressed (hourly rows upserted),
            rows_compressed (raw rows aggregated), raw_rows_pruned and
            hourly_rows_pruned.
        """
        now = now or self._now()
        self._warn_if_compression_precedes_recompute()
        stats = {
            "hours_compressed": 0,
            "rows_compressed": 0,
            "raw_rows_pruned": 0,
            "hourly_rows_pruned": 0,
        }

        # Roll up before compression and pruning, while the last day is still raw.
        if self._rollup_enabled:
            try:
                await self.refresh_usage_rollup(now)
            except Exception as e:
                logger.error("Workload usage rollup failed: %s", e)

        try:
            compressed = await self._compress_to_hourly(now)
            stats["hours_compressed"] = compressed["hours"]
            stats["rows_compressed"] = compressed["rows"]
        except Exception as e:
            logger.error("Metrics compression failed: %s", e)

        try:
            stats["raw_rows_pruned"] = await self._prune_raw(now)
        except Exception as e:
            logger.error("Raw metrics pruning failed: %s", e)

        try:
            stats["hourly_rows_pruned"] = await self._prune_hourly(now)
        except Exception as e:
            logger.error("Hourly metrics pruning failed: %s", e)

        if self._rollup_enabled:
            try:
                await self._prune_usage_rollup(now)
            except Exception as e:
                logger.error("Workload usage rollup pruning failed: %s", e)

        # Reclaim dead tuple space so the PVC can be reused for new data.
        try:
            await self._vacuum_main_tables()
        except Exception as e:
            logger.error("Vacuum failed: %s", e)

        logger.info(
            "Compression cycle complete: %d hourly rows, %d raw rows compressed, %d raw pruned, %d hourly pruned",
            stats["hours_compressed"],
            stats["rows_compressed"],
            stats["raw_rows_pruned"],
            stats["hourly_rows_pruned"],
        )
        return stats

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _warn_if_compression_precedes_recompute(self) -> None:
        age = self._config.METRICS_COMPRESSION_AGE_HOURS
        if age >= CARBON_RECOMPUTE_HORIZON_HOURS or MetricsCompressor._short_age_warned:
            return
        MetricsCompressor._short_age_warned = True
        logger.warning(
            "METRICS_COMPRESSION_AGE_HOURS=%d is below the %dh carbon-intensity recompute window: "
            "hours compressed before their intensity is consolidated keep their provisional or "
            "default carbon figures. Use %d or more.",
            age,
            CARBON_RECOMPUTE_HORIZON_HOURS,
            CARBON_RECOMPUTE_HORIZON_HOURS,
        )

    async def _compression_limit(self, now: datetime) -> datetime:
        """Exclusive end of the closed hours that may be compressed."""
        limit = now - timedelta(hours=self._config.METRICS_COMPRESSION_AGE_HOURS)
        if self._collection_watermark_provider is not None:
            try:
                watermark = await self._collection_watermark_provider()
            except Exception as exc:
                logger.warning("Collection watermark unavailable; compression waits for it: %s", exc)
                raise
            if watermark is not None:
                limit = min(limit, watermark)
        return floor_hour(limit)

    # ------------------------------------------------------------------
    # Compression: raw → hourly
    # ------------------------------------------------------------------

    async def _compress_to_hourly(self, now: datetime | None = None) -> dict:
        """Aggregate every closed, not yet compressed hour of each metrics table."""
        now = now or self._now()
        upper = await self._compression_limit(now)
        hours = 0
        rows = 0
        for table in TABLES:
            lower = await self._compression_lower_bound(table, upper)
            if lower is None or lower >= upper:
                continue
            chunk_start = lower
            while chunk_start < upper:
                chunk_end = min(chunk_start + timedelta(hours=MAX_HOURS_PER_TRANSACTION), upper)
                upserted, aggregated = await self._compress_chunk(table, chunk_start, chunk_end, now)
                hours += upserted
                rows += aggregated
                chunk_start = chunk_end
        return {"hours": hours, "rows": rows}

    async def _compression_lower_bound(self, table: _MetricsTable, upper: datetime) -> Optional[datetime]:
        """Return where compression resumes: the watermark, or the first complete raw hour."""
        watermark = await self.get_watermark(table.raw)
        if watermark is not None:
            return watermark
        first_raw = await self._min_raw_timestamp(table)
        if first_raw is None:
            return None
        lower = floor_hour(first_raw)
        # Installs upgraded from the legacy compressor may hold a first raw hour
        # that was partially pruned at a non-aligned cutoff. Its complete
        # aggregate already lives in the hourly table: never overwrite it.
        if first_raw > lower and await self._hourly_bucket_exists(table, lower):
            lower += timedelta(hours=1)
        return min(lower, upper)

    async def _compress_chunk(
        self, table: _MetricsTable, start: datetime, end: datetime, now: datetime
    ) -> tuple[int, int]:
        """Aggregate ``[start, end)`` and advance the watermark in one transaction."""
        backend = self._db.db_type
        sql = table.compress_sql(backend)
        if backend == "postgres":
            async with self._db.connection_scope() as conn:
                async with conn.transaction():
                    status = await conn.execute(sql, start, end)
                    aggregated = await conn.fetchval(
                        f"SELECT COALESCE(SUM(sample_count), 0) FROM {table.hourly} "
                        "WHERE hour_bucket >= $1 AND hour_bucket < $2",
                        start,
                        end,
                    )
                    await conn.execute(
                        """
                        INSERT INTO metrics_compression_watermarks (table_name, compressed_until, updated_at)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (table_name) DO UPDATE SET
                            compressed_until = EXCLUDED.compressed_until,
                            updated_at = EXCLUDED.updated_at
                        """,
                        table.raw,
                        end,
                        now,
                    )
            return _status_count(status), int(aggregated or 0)

        start_iso, end_iso = to_iso_z(start), to_iso_z(end)
        async with self._db.connection_scope() as conn:
            try:
                cursor = await conn.execute(
                    sql,
                    (
                        to_iso_z(start - timedelta(seconds=1)),
                        to_iso_z(end + timedelta(seconds=1)),
                        start_iso,
                        end_iso,
                    ),
                )
                upserted = max(cursor.rowcount, 0)
                async with conn.execute(
                    f"SELECT COALESCE(SUM(sample_count), 0) FROM {table.hourly} "
                    "WHERE hour_bucket >= ? AND hour_bucket < ?",
                    (start_iso, end_iso),
                ) as count_cursor:
                    aggregated = (await count_cursor.fetchone())[0]
                await conn.execute(
                    """
                    INSERT INTO metrics_compression_watermarks (table_name, compressed_until, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (table_name) DO UPDATE SET
                        compressed_until = excluded.compressed_until,
                        updated_at = excluded.updated_at
                    """,
                    (table.raw, end_iso, to_iso_z(now)),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return upserted, int(aggregated or 0)

    async def get_watermark(self, table_name: str) -> Optional[datetime]:
        """Return the exclusive end of the compressed hours of ``table_name``."""
        if self._db.db_type == "postgres":
            async with self._db.connection_scope() as conn:
                value = await conn.fetchval(
                    "SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = $1",
                    table_name,
                )
            return value.astimezone(timezone.utc) if value is not None else None
        async with self._db.connection_scope() as conn:
            async with conn.execute(
                "SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = ?",
                (table_name,),
            ) as cursor:
                row = await cursor.fetchone()
        return _parse_utc(row[0]) if row and row[0] else None

    async def _min_raw_timestamp(self, table: _MetricsTable) -> Optional[datetime]:
        backend = self._db.db_type
        ts = table.timestamp_column(backend)
        async with self._db.connection_scope() as conn:
            if backend == "postgres":
                value = await conn.fetchval(f"SELECT MIN({ts}) FROM {table.raw}")
                return value.astimezone(timezone.utc) if value is not None else None
            async with conn.execute(f"SELECT MIN({ts}) FROM {table.raw}") as cursor:
                row = await cursor.fetchone()
        return _parse_utc(row[0]) if row and row[0] else None

    async def _hourly_bucket_exists(self, table: _MetricsTable, bucket: datetime) -> bool:
        async with self._db.connection_scope() as conn:
            if self._db.db_type == "postgres":
                return bool(await conn.fetchval(f"SELECT 1 FROM {table.hourly} WHERE hour_bucket = $1 LIMIT 1", bucket))
            async with conn.execute(
                f"SELECT 1 FROM {table.hourly} WHERE hour_bucket = ? LIMIT 1", (to_iso_z(bucket),)
            ) as cursor:
                return await cursor.fetchone() is not None

    # ------------------------------------------------------------------
    # Workload usage rollup (roadmap E3)
    # ------------------------------------------------------------------

    def _usage_rollup(self):
        from ..core.workload_usage import WorkloadUsageRollup
        from ..storage.base_workload_usage_repository import create_workload_usage_repository
        from ..utils.time import parse_query_step_seconds

        repository = create_workload_usage_repository(self._db, self._db.db_type)
        lookback_days = max(int(getattr(self._config, "RECOMMENDATION_LOOKBACK_DAYS", 14) or 14), 1)
        return WorkloadUsageRollup(
            repository,
            step_seconds=parse_query_step_seconds(self._config.PROMETHEUS_QUERY_RANGE_STEP),
            backfill_hours=lookback_days * 24,
        )

    async def refresh_usage_rollup(self, now: datetime | None = None, *, refresh_hours: int | None = None):
        """Re-roll the recent hours of ``workload_usage_hourly`` (and backfill it on first run)."""
        from ..core.workload_usage import DEFAULT_REFRESH_HOURS

        rollup = self._usage_rollup()
        result = await rollup.refresh(
            now or self._now(),
            refresh_hours=refresh_hours if refresh_hours is not None else DEFAULT_REFRESH_HOURS,
        )
        if result.hours:
            logger.info(
                "Workload usage rollup: %d hours rolled into %d rows (%d late hours).",
                result.hours,
                result.rows,
                result.late_hours,
            )
        return result

    async def _prune_usage_rollup(self, now: datetime) -> int:
        """Prune the rollup like the hourly aggregates (METRICS_AGGREGATED_RETENTION_DAYS)."""
        retention_days = self._config.METRICS_AGGREGATED_RETENTION_DAYS
        if retention_days < 0:
            return 0
        cutoff = floor_hour(now - timedelta(days=retention_days))
        return await self._usage_rollup().repository.prune_before(cutoff)

    # ------------------------------------------------------------------
    # Retention: prune old raw rows
    # ------------------------------------------------------------------

    async def _prune_raw(self, now: datetime | None = None) -> int:
        """Delete raw rows older than METRICS_RAW_RETENTION_DAYS, by whole compressed hours.

        Set to -1 to disable pruning (keep raw data indefinitely). Rows at or
        after a table's compression watermark are never deleted, whatever the
        retention, so uncompressed hours cannot be lost.
        """
        retention_days = self._config.METRICS_RAW_RETENTION_DAYS
        if retention_days < 0:
            return 0
        now = now or self._now()
        retention_cutoff = floor_hour(now - timedelta(days=retention_days))
        total = 0
        for table in TABLES:
            watermark = await self.get_watermark(table.raw)
            if watermark is None:
                continue
            cutoff = min(retention_cutoff, watermark)
            total += await self._delete_raw_before(table, cutoff)
        if total:
            logger.info("Pruned %d raw metrics older than %d days", total, retention_days)
        return total

    async def _delete_raw_before(self, table: _MetricsTable, cutoff: datetime) -> int:
        backend = self._db.db_type
        ts = table.timestamp_column(backend)
        async with self._db.connection_scope() as conn:
            if backend == "postgres":
                status = await conn.execute(f"DELETE FROM {table.raw} WHERE {ts} < $1", cutoff)
                return _status_count(status)
            cursor = await conn.execute(
                f"DELETE FROM {table.raw} WHERE {ts} < ? AND {table.hour_expr(backend)} < ?",
                (to_iso_z(cutoff + timedelta(seconds=1)), to_iso_z(cutoff)),
            )
            await conn.commit()
            return max(cursor.rowcount, 0)

    async def _prune_hourly(self, now: datetime | None = None) -> int:
        """Delete hourly aggregates older than METRICS_AGGREGATED_RETENTION_DAYS.

        Set to -1 to disable pruning (keep aggregated data indefinitely).
        """
        retention_days = self._config.METRICS_AGGREGATED_RETENTION_DAYS
        if retention_days < 0:
            return 0

        cutoff = floor_hour((now or self._now()) - timedelta(days=retention_days))
        total = 0
        async with self._db.connection_scope() as conn:
            for table in TABLES:
                if self._db.db_type == "postgres":
                    status = await conn.execute(f"DELETE FROM {table.hourly} WHERE hour_bucket < $1", cutoff)
                    total += _status_count(status)
                else:
                    cursor = await conn.execute(
                        f"DELETE FROM {table.hourly} WHERE hour_bucket < ?",
                        (to_iso_z(cutoff),),
                    )
                    total += max(cursor.rowcount, 0)
            if self._db.db_type != "postgres":
                await conn.commit()
        if total:
            logger.info("Pruned %d hourly metrics older than %d days", total, retention_days)
        return total

    # ------------------------------------------------------------------
    # Namespace cache maintenance
    # ------------------------------------------------------------------

    async def refresh_namespace_cache(self) -> int:
        """Update the namespace_cache table from recent raw metrics."""
        if self._db.db_type == "postgres":
            async with self._db.connection_scope() as conn:
                result = await conn.execute(
                    """
                    INSERT INTO namespace_cache (namespace, last_seen)
                    SELECT DISTINCT namespace, NOW()
                    FROM combined_metrics
                    WHERE timestamp > NOW() - INTERVAL '7 days'
                    ON CONFLICT (namespace) DO UPDATE SET last_seen = EXCLUDED.last_seen
                    """
                )
                return _status_count(result)
        async with self._db.connection_scope() as conn:
            recent_cutoff = to_iso_z(datetime.now(timezone.utc) - timedelta(days=7))
            cursor = await conn.execute(
                """
                INSERT OR REPLACE INTO namespace_cache (namespace, last_seen)
                SELECT DISTINCT namespace, ?
                FROM combined_metrics
                WHERE "timestamp" > ?
                """,
                (to_iso_z(datetime.now(timezone.utc)), recent_cutoff),
            )
            count = cursor.rowcount
            await conn.commit()
            return count

    # ------------------------------------------------------------------
    # PostgreSQL maintenance
    # ------------------------------------------------------------------

    async def _vacuum_main_tables(self) -> None:
        """Run VACUUM on main tables to reclaim dead tuple space.

        UPSERT-heavy tables accumulate dead tuples from UPDATE operations.
        VACUUM marks this space as reusable, preventing unbounded table bloat.
        """
        if self._db.db_type != "postgres":
            return

        async with self._db.connection_scope() as conn:
            # VACUUM (without FULL) does not lock the table and is safe for
            # production. It reclaims dead tuple space for reuse by future
            # INSERTs/UPDATEs.
            for table in (
                "combined_metrics",
                "combined_metrics_hourly",
                "container_metrics",
                "container_metrics_hourly",
                "workload_usage_hourly",
                "carbon_intensity_history",
                "node_snapshots",
                "node_snapshots_scd",
                "recommendation_history",
                "recommendation_savings_ledger",
                "recommendation_savings_ledger_hourly",
            ):
                try:
                    await conn.execute(f"VACUUM {table}")
                except Exception:
                    logger.debug("VACUUM %s skipped (table may not exist yet)", table)


def _status_count(status: Any) -> int:
    """Parse the affected-row count of an asyncpg status string such as ``INSERT 0 7``."""
    if not status or not isinstance(status, str):
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
