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
  deleted — never half pruned;
* the scheduled job passes :func:`collection_limit_provider`: the collection
  watermark, lowered to the first failed or cost-pending step (STO-02);
* a raw row inserted, updated or deleted below the watermark marks its hour
  in ``metrics_dirty_hours`` (database triggers, migration 0170); each run
  rebuilds the hourly rows of those hours from their raw rows while they are
  complete (not below ``raw_pruned_until``), before pruning (ROL-02).

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
from ..models.provenance import cost_source_aggregate_sql
from ..utils.date_utils import to_iso_z

logger = logging.getLogger(__name__)

#: Compression lag above which health reports the compressor as degraded (ROL-01).
COMPRESSION_LAG_DEGRADED_SECONDS = 2 * 3600
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


#: Duration-weighted average GPU power of an hour (B5, migration 0064): times
#: the hourly ``duration_seconds`` it gives back the GPU energy of the raw
#: rows, which the workload usage rollup re-reads once raw rows are pruned.
#: Rows without GPU power count as 0 W when others in the hour have some, and
#: rows without a duration fall back to the plain average (one step each).
_GPU_POWER_AVERAGE = (
    "CASE WHEN COUNT(gpu_power_watts) = 0 THEN NULL "
    "WHEN SUM(COALESCE(duration_seconds, 0)) > 0 THEN "
    "SUM(COALESCE(gpu_power_watts, 0) * COALESCE(duration_seconds, 0)) * 1.0 / SUM(COALESCE(duration_seconds, 0)) "
    "ELSE SUM(COALESCE(gpu_power_watts, 0)) * 1.0 / COUNT(*) END"
)


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
            # opencost + opencost_derived stays a trusted OpenCost cost, not ``mixed``.
            ("cost_source", cost_source_aggregate_sql("cost_source")),
            ("usage_source", _source_aggregate("usage_source")),
            ("power_source", _source_aggregate("power_source")),
            ("intensity_source", _source_aggregate("intensity_source")),
            ("emaps_zone", _mode("emaps_zone", backend)),
            ("gpu_power_watts", _GPU_POWER_AVERAGE),
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

#: Raw ``__idle__`` rows per node and step (B2, migration 0050), pruned with ``combined_metrics``.
IDLE_TABLE = "node_idle_metrics"

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
        collection_watermark_provider: CollectionWatermarkProvider | None,
        rollup_enabled: bool = True,
    ):
        """
        Args:
            db_manager: The database manager.
            config: Optional configuration override.
            collection_watermark_provider: Coroutine returning the end of the
                last completely collected window (lowered to the first failed
                or cost-pending step, see :func:`collection_limit_provider`).
                Compression never passes it, so hours that are still being
                (back)filled stay raw. Mandatory (STO-02): pass ``None``
                explicitly only where no collection job writes the tables.
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

        failures: list[BaseException] = []

        async def step(name: str, action: Callable[[], Awaitable[Any]]) -> Any:
            try:
                return await action()
            except Exception as e:
                logger.error("%s failed: %s", name, e)
                failures.append(e)
                return None

        # Roll up before compression and pruning, while the last day is still raw.
        if self._rollup_enabled:
            await step("Workload usage rollup", lambda: self.refresh_usage_rollup(now))

        reaggregated = await step("Dirty hour re-aggregation", self._reaggregate_dirty_hours)
        stats["hours_reaggregated"] = reaggregated or 0
        compressed = await step("Metrics compression", lambda: self._compress_to_hourly(now))
        if compressed:
            stats["hours_compressed"] = compressed["hours"]
            stats["rows_compressed"] = compressed["rows"]
        stats["raw_rows_pruned"] = await step("Raw metrics pruning", lambda: self._prune_raw(now)) or 0
        stats["hourly_rows_pruned"] = await step("Hourly metrics pruning", lambda: self._prune_hourly(now)) or 0
        if self._rollup_enabled:
            await step("Workload usage rollup pruning", lambda: self._prune_usage_rollup(now))
        # Reclaim dead tuple space so the PVC can be reused for new data.
        await step("Vacuum", self._vacuum_main_tables)
        await step("Compression lag export", lambda: self.compression_status(now))

        if failures:
            # A swallowed failure must still show in the loop status and metrics (ROL-01).
            from .loops import report_loop_failure

            report_loop_failure(failures[0])

        logger.info(
            "Compression cycle complete: %d hourly rows, %d raw rows compressed, %d raw pruned, %d hourly pruned",
            stats["hours_compressed"],
            stats["rows_compressed"],
            stats["raw_rows_pruned"],
            stats["hourly_rows_pruned"],
        )
        return stats

    async def compression_status(self, now: datetime | None = None) -> dict[str, Any]:
        """Return the compression watermark and lag of each metrics table and export the lag gauge.

        The lag is how far the watermark trails the last hour the compressor may
        reach (``floor_hour(now - METRICS_COMPRESSION_AGE_HOURS)``). Readers never
        lose rows when it grows (they split at the watermark), but raw retention
        and query cost do: a lag above :data:`COMPRESSION_LAG_DEGRADED_SECONDS`
        reports ``degraded``.
        """
        from .observability import METRICS_COMPRESSION_LAG

        now = now or self._now()
        reachable = floor_hour(now - timedelta(hours=self._config.METRICS_COMPRESSION_AGE_HOURS))
        tables: dict[str, Any] = {}
        worst: Optional[float] = None
        for table in TABLES:
            watermark = await self.get_watermark(table.raw)
            lag = max((reachable - watermark).total_seconds(), 0.0) if watermark is not None else None
            if lag is not None:
                METRICS_COMPRESSION_LAG.labels(table=table.raw).set(lag)
                worst = lag if worst is None else max(worst, lag)
            tables[table.raw] = {"watermark": watermark, "lag_seconds": lag}
        degraded = worst is not None and worst > COMPRESSION_LAG_DEGRADED_SECONDS
        return {"status": "degraded" if degraded else "ok", "lag_seconds": worst, "tables": tables}

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
    # Late rows: re-aggregate dirty hours (ROL-02, STO-02)
    # ------------------------------------------------------------------

    async def _reaggregate_dirty_hours(self) -> int:
        """Rebuild the hourly rows of every compressed hour whose raw rows changed after compression.

        Database triggers (migration 0170) mark an hour in ``metrics_dirty_hours``
        when a raw row below the compression watermark is inserted, updated or
        deleted. An hour whose raw rows are still complete (at or after the
        table's ``raw_pruned_until``) is rebuilt from them in one transaction:
        its hourly rows are deleted and aggregated again. An hour already pruned
        keeps its complete aggregate: the late rows alone would replace it with
        a partial one, so they are left out with a warning and pruned.

        Returns the number of hours rebuilt.
        """
        rebuilt = 0
        for table in TABLES:
            hours = await self._dirty_hours(table)
            if not hours:
                continue
            watermark = await self.get_watermark(table.raw)
            pruned_until = await self._raw_pruned_until(table)
            stranded = 0
            for hour in hours:
                if watermark is None or hour >= watermark:
                    await self._clear_dirty(table, hour)  # not compressed yet: read and compressed from raw rows
                elif pruned_until is not None and hour < pruned_until:
                    stranded += 1
                    await self._clear_dirty(table, hour)
                else:
                    await self._rebuild_hour(table, hour)
                    rebuilt += 1
            if stranded:
                logger.warning(
                    "%d hour(s) of %s received rows after their raw rows were pruned; their hourly "
                    "aggregates are kept and the late rows are left out (ROL-02).",
                    stranded,
                    table.raw,
                )
        if rebuilt:
            logger.info("Re-aggregated %d compressed hour(s) that received late rows.", rebuilt)
        return rebuilt

    async def _dirty_hours(self, table: _MetricsTable) -> list[datetime]:
        async with self._db.connection_scope() as conn:
            if self._db.db_type == "postgres":
                rows = await conn.fetch(
                    "SELECT hour_bucket FROM metrics_dirty_hours WHERE table_name = $1 ORDER BY hour_bucket", table.raw
                )
                return [_parse_utc(row[0]) for row in rows]
            async with conn.execute(
                "SELECT hour_bucket FROM metrics_dirty_hours WHERE table_name = ? ORDER BY hour_bucket", (table.raw,)
            ) as cursor:
                return [_parse_utc(row[0]) for row in await cursor.fetchall()]

    async def _clear_dirty(self, table: _MetricsTable, hour: datetime) -> None:
        async with self._db.connection_scope() as conn:
            if self._db.db_type == "postgres":
                await conn.execute(
                    "DELETE FROM metrics_dirty_hours WHERE table_name = $1 AND hour_bucket = $2", table.raw, hour
                )
                return
            await conn.execute(
                "DELETE FROM metrics_dirty_hours WHERE table_name = ? AND hour_bucket = ?", (table.raw, to_iso_z(hour))
            )
            await conn.commit()

    async def _raw_pruned_until(self, table: _MetricsTable) -> Optional[datetime]:
        async with self._db.connection_scope() as conn:
            if self._db.db_type == "postgres":
                value = await conn.fetchval(
                    "SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = $1", table.raw
                )
                return _parse_utc(value) if value is not None else None
            async with conn.execute(
                "SELECT raw_pruned_until FROM metrics_compression_watermarks WHERE table_name = ?", (table.raw,)
            ) as cursor:
                row = await cursor.fetchone()
        return _parse_utc(row[0]) if row and row[0] else None

    async def _rebuild_hour(self, table: _MetricsTable, hour: datetime) -> None:
        """Replace the hourly rows of ``hour`` by the aggregate of its raw rows, in one transaction.

        The dirty mark is removed first: a raw write committed after the
        aggregate read marks the hour again for the next run.
        """
        end = hour + timedelta(hours=1)
        backend = self._db.db_type
        sql = table.compress_sql(backend)
        if backend == "postgres":
            async with self._db.connection_scope() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM metrics_dirty_hours WHERE table_name = $1 AND hour_bucket = $2", table.raw, hour
                    )
                    await conn.execute(f"DELETE FROM {table.hourly} WHERE hour_bucket = $1", hour)
                    await conn.execute(sql, hour, end)
            return
        async with self._db.connection_scope() as conn:
            try:
                await conn.execute(
                    "DELETE FROM metrics_dirty_hours WHERE table_name = ? AND hour_bucket = ?",
                    (table.raw, to_iso_z(hour)),
                )
                await conn.execute(f"DELETE FROM {table.hourly} WHERE hour_bucket = ?", (to_iso_z(hour),))
                await conn.execute(
                    sql,
                    (
                        to_iso_z(hour - timedelta(seconds=1)),
                        to_iso_z(end + timedelta(seconds=1)),
                        to_iso_z(hour),
                        to_iso_z(end),
                    ),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

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
            warmup_seconds=int(getattr(self._config, "RIGHTSIZING_WARMUP_MINUTES", 0) or 0) * 60,
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

        The ``__idle__`` rows of ``node_idle_metrics`` (B2) are raw rows too:
        they are pruned with the pod rows of ``combined_metrics`` (same
        retention, same watermark), so every pod step kept in raw form keeps
        the idle rows that complete its node totals, for ``greenkube recompute``.
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
            dirty = await self._dirty_hours(table)
            if dirty:
                cutoff = min(cutoff, dirty[0])  # never delete raw rows a re-aggregation still needs
            total += await self._delete_raw_before(table, cutoff)
            if table.pod_scope:
                total += await self._delete_idle_before(cutoff)
        if total:
            logger.info("Pruned %d raw metrics older than %d days", total, retention_days)
        return total

    async def _delete_raw_before(self, table: _MetricsTable, cutoff: datetime) -> int:
        """Delete the raw rows of the hours before ``cutoff`` and advance ``raw_pruned_until``.

        Both happen in one transaction, the mark first, so the dirty-hour
        triggers ignore the pruning deletes (ROL-02).
        """
        backend = self._db.db_type
        ts = table.timestamp_column(backend)
        async with self._db.connection_scope() as conn:
            if backend == "postgres":
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE metrics_compression_watermarks SET raw_pruned_until = $2 "
                        "WHERE table_name = $1 AND (raw_pruned_until IS NULL OR raw_pruned_until < $2)",
                        table.raw,
                        cutoff,
                    )
                    status = await conn.execute(f"DELETE FROM {table.raw} WHERE {ts} < $1", cutoff)
                return _status_count(status)
            try:
                await conn.execute(
                    "UPDATE metrics_compression_watermarks SET raw_pruned_until = ? "
                    "WHERE table_name = ? AND (raw_pruned_until IS NULL OR raw_pruned_until < ?)",
                    (to_iso_z(cutoff), table.raw, to_iso_z(cutoff)),
                )
                cursor = await conn.execute(
                    f"DELETE FROM {table.raw} WHERE {ts} < ? AND {table.hour_expr(backend)} < ?",
                    (to_iso_z(cutoff + timedelta(seconds=1)), to_iso_z(cutoff)),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            return max(cursor.rowcount, 0)

    async def _delete_idle_before(self, cutoff: datetime) -> int:
        """Delete ``__idle__`` rows of steps before ``cutoff`` (an hour boundary)."""
        async with self._db.connection_scope() as conn:
            if self._db.db_type == "postgres":
                status = await conn.execute(f"DELETE FROM {IDLE_TABLE} WHERE timestamp < $1", cutoff)
                return _status_count(status)
            # Stored timestamps are ISO strings: compare on the hour bucket, like raw rows.
            cursor = await conn.execute(
                f'DELETE FROM {IDLE_TABLE} WHERE "timestamp" < ? AND {_SQLITE_HOUR.format(column='"timestamp"')} < ?',
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


def collection_limit_provider(db_manager: Any, config: Config | None = None) -> CollectionWatermarkProvider:
    """Build the compression limit of the scheduled job from the metric collection (STO-02).

    The coroutine returns the collection watermark (``last_complete_end`` of
    ``combined_metrics``), lowered to the start of the earliest step that is
    still ``failed`` or whose cost is ``opencost_pending`` within the retry
    horizon (``COLLECTION_MAX_BACKFILL`` before the watermark): those steps are
    rewritten later and their hour stays raw until then. Without a watermark it
    returns the epoch, so nothing is compressed before the first complete window.
    """
    cfg = config or get_config()

    async def provider() -> Optional[datetime]:
        from ..storage.base_collection_watermark_repository import create_collection_watermark_repository
        from ..storage.base_collection_window_repository import (
            COST_STATUS_PENDING,
            STATUS_FAILED,
            create_collection_window_repository,
        )
        from ..storage.installation_repository import create_installation_repository
        from ..utils.date_utils import parse_duration
        from .cluster_identity import cached_installation_identity
        from .collection import METRICS_WATERMARK_SOURCE, collection_cluster_key

        cached = cached_installation_identity()
        if cached is not None:
            cluster_uid = cached.cluster_uid
        else:
            stored = await create_installation_repository(db_manager, db_manager.db_type).get()
            cluster_uid = stored.cluster_uid if stored is not None else collection_cluster_key(cfg)
        watermark = await create_collection_watermark_repository(db_manager, db_manager.db_type).get(
            cluster_uid, METRICS_WATERMARK_SOURCE
        )
        if watermark is None:
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        limit = watermark.last_complete_end
        horizon = limit - parse_duration(str(getattr(cfg, "COLLECTION_MAX_BACKFILL", "24h") or "24h"))
        records = await create_collection_window_repository(db_manager, db_manager.db_type).list_overlapping(
            cluster_uid, METRICS_WATERMARK_SOURCE, horizon, limit
        )
        for record in records:
            if record.status == STATUS_FAILED or record.cost_status == COST_STATUS_PENDING:
                limit = min(limit, record.window_start)
        return limit

    return provider


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
