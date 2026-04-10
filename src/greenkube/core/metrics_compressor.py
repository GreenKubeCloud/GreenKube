# src/greenkube/core/metrics_compressor.py
"""
Metrics compression service.

Compresses raw 5-minute combined_metrics into hourly aggregates
in the combined_metrics_hourly table, then optionally prunes stale
raw rows. This prevents unbounded data growth and eliminates OOM
issues when the API loads metrics for reports.
"""

import logging
from datetime import datetime, timedelta, timezone

from ..core.config import Config, get_config

logger = logging.getLogger(__name__)


class MetricsCompressor:
    """Compresses raw metrics into hourly aggregates and manages retention."""

    def __init__(self, db_manager, config: Config | None = None):
        self._db = db_manager
        self._config = config or get_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """Execute the full compression + retention cycle.

        Returns:
            Dict with keys: hours_compressed, rows_compressed, raw_rows_pruned.
        """
        stats = {
            "hours_compressed": 0,
            "rows_compressed": 0,
            "raw_rows_pruned": 0,
            "hourly_rows_pruned": 0,
        }

        try:
            compressed = await self._compress_to_hourly()
            stats["hours_compressed"] = compressed["hours"]
            stats["rows_compressed"] = compressed["rows"]
        except Exception as e:
            logger.error("Metrics compression failed: %s", e)

        try:
            stats["raw_rows_pruned"] = await self._prune_raw()
        except Exception as e:
            logger.error("Raw metrics pruning failed: %s", e)

        try:
            stats["hourly_rows_pruned"] = await self._prune_hourly()
        except Exception as e:
            logger.error("Hourly metrics pruning failed: %s", e)

        logger.info(
            "Compression cycle complete: %d hours, %d raw rows compressed, %d raw pruned, %d hourly pruned",
            stats["hours_compressed"],
            stats["rows_compressed"],
            stats["raw_rows_pruned"],
            stats["hourly_rows_pruned"],
        )
        return stats

    # ------------------------------------------------------------------
    # Compression: raw → hourly
    # ------------------------------------------------------------------

    async def _compress_to_hourly(self) -> dict:
        """Aggregate raw rows older than the compression threshold into hourly buckets."""
        age_hours = self._config.METRICS_COMPRESSION_AGE_HOURS
        cutoff = datetime.now(timezone.utc) - timedelta(hours=age_hours)

        if self._db.db_type == "postgres":
            return await self._compress_postgres(cutoff)
        return await self._compress_sqlite(cutoff)

    async def _compress_postgres(self, cutoff: datetime) -> dict:
        """PostgreSQL compression using INSERT ... SELECT with aggregation."""
        async with self._db.connection_scope() as conn:
            result = await conn.execute(
                """
                INSERT INTO combined_metrics_hourly (
                    pod_name, namespace, hour_bucket, sample_count,
                    total_cost, co2e_grams, embodied_co2e_grams,
                    pue, grid_intensity, joules,
                    cpu_request, memory_request,
                    cpu_usage_avg, cpu_usage_max,
                    memory_usage_avg, memory_usage_max,
                    network_receive_bytes, network_transmit_bytes,
                    disk_read_bytes, disk_write_bytes,
                    storage_request_bytes, storage_usage_bytes,
                    gpu_usage_millicores, restart_count,
                    owner_kind, owner_name,
                    duration_seconds, node, node_instance_type,
                    node_zone, emaps_zone, is_estimated,
                    estimation_reasons, calculation_version
                )
                SELECT
                    pod_name,
                    namespace,
                    date_trunc('hour', timestamp) AS hour_bucket,
                    COUNT(*)                      AS sample_count,
                    SUM(total_cost)               AS total_cost,
                    SUM(co2e_grams)               AS co2e_grams,
                    SUM(COALESCE(embodied_co2e_grams, 0)) AS embodied_co2e_grams,
                    AVG(pue)                      AS pue,
                    AVG(grid_intensity)           AS grid_intensity,
                    SUM(joules)                   AS joules,
                    MAX(cpu_request)              AS cpu_request,
                    MAX(memory_request)           AS memory_request,
                    AVG(cpu_usage_millicores)::INTEGER AS cpu_usage_avg,
                    MAX(cpu_usage_millicores)     AS cpu_usage_max,
                    AVG(memory_usage_bytes)::BIGINT AS memory_usage_avg,
                    MAX(memory_usage_bytes)       AS memory_usage_max,
                    AVG(network_receive_bytes)    AS network_receive_bytes,
                    AVG(network_transmit_bytes)   AS network_transmit_bytes,
                    AVG(disk_read_bytes)          AS disk_read_bytes,
                    AVG(disk_write_bytes)         AS disk_write_bytes,
                    MAX(storage_request_bytes)    AS storage_request_bytes,
                    MAX(storage_usage_bytes)      AS storage_usage_bytes,
                    MAX(gpu_usage_millicores)     AS gpu_usage_millicores,
                    MAX(restart_count)            AS restart_count,
                    MODE() WITHIN GROUP (ORDER BY owner_kind) AS owner_kind,
                    MODE() WITHIN GROUP (ORDER BY owner_name) AS owner_name,
                    SUM(COALESCE(duration_seconds, 0)) AS duration_seconds,
                    MODE() WITHIN GROUP (ORDER BY node)  AS node,
                    MODE() WITHIN GROUP (ORDER BY node_instance_type) AS node_instance_type,
                    MODE() WITHIN GROUP (ORDER BY node_zone) AS node_zone,
                    MODE() WITHIN GROUP (ORDER BY emaps_zone) AS emaps_zone,
                    BOOL_OR(COALESCE(is_estimated, FALSE)) AS is_estimated,
                    '[]'                          AS estimation_reasons,
                    MAX(calculation_version)      AS calculation_version
                FROM combined_metrics
                WHERE timestamp < $1
                GROUP BY pod_name, namespace, date_trunc('hour', timestamp)
                ON CONFLICT (pod_name, namespace, hour_bucket) DO UPDATE SET
                    sample_count     = EXCLUDED.sample_count,
                    total_cost       = EXCLUDED.total_cost,
                    co2e_grams       = EXCLUDED.co2e_grams,
                    embodied_co2e_grams = EXCLUDED.embodied_co2e_grams,
                    pue              = EXCLUDED.pue,
                    grid_intensity   = EXCLUDED.grid_intensity,
                    joules           = EXCLUDED.joules,
                    cpu_request      = EXCLUDED.cpu_request,
                    memory_request   = EXCLUDED.memory_request,
                    cpu_usage_avg    = EXCLUDED.cpu_usage_avg,
                    cpu_usage_max    = EXCLUDED.cpu_usage_max,
                    memory_usage_avg = EXCLUDED.memory_usage_avg,
                    memory_usage_max = EXCLUDED.memory_usage_max,
                    duration_seconds = EXCLUDED.duration_seconds
                """,
                cutoff,
            )
            # asyncpg returns status string like "INSERT 0 42"
            count = int(result.split()[-1]) if result else 0
            return {"hours": count, "rows": count}

    async def _compress_sqlite(self, cutoff: datetime) -> dict:
        """SQLite compression using INSERT OR REPLACE with aggregation."""
        cutoff_iso = cutoff.isoformat()
        async with self._db.connection_scope() as conn:
            cursor = await conn.execute(
                """
                INSERT OR REPLACE INTO combined_metrics_hourly (
                    pod_name, namespace, hour_bucket, sample_count,
                    total_cost, co2e_grams, embodied_co2e_grams,
                    pue, grid_intensity, joules,
                    cpu_request, memory_request,
                    cpu_usage_avg, cpu_usage_max,
                    memory_usage_avg, memory_usage_max,
                    network_receive_bytes, network_transmit_bytes,
                    disk_read_bytes, disk_write_bytes,
                    storage_request_bytes, storage_usage_bytes,
                    gpu_usage_millicores, restart_count,
                    owner_kind, owner_name,
                    duration_seconds, node, node_instance_type,
                    node_zone, emaps_zone, is_estimated,
                    estimation_reasons, calculation_version
                )
                SELECT
                    pod_name,
                    namespace,
                    strftime('%Y-%m-%dT%H:00:00Z', "timestamp") AS hour_bucket,
                    COUNT(*)                      AS sample_count,
                    SUM(total_cost)               AS total_cost,
                    SUM(co2e_grams)               AS co2e_grams,
                    SUM(COALESCE(embodied_co2e_grams, 0)) AS embodied_co2e_grams,
                    AVG(pue)                      AS pue,
                    AVG(grid_intensity)           AS grid_intensity,
                    SUM(joules)                   AS joules,
                    MAX(cpu_request)              AS cpu_request,
                    MAX(memory_request)           AS memory_request,
                    CAST(AVG(cpu_usage_millicores) AS INTEGER) AS cpu_usage_avg,
                    MAX(cpu_usage_millicores)     AS cpu_usage_max,
                    CAST(AVG(memory_usage_bytes) AS INTEGER) AS memory_usage_avg,
                    MAX(memory_usage_bytes)       AS memory_usage_max,
                    AVG(network_receive_bytes)    AS network_receive_bytes,
                    AVG(network_transmit_bytes)   AS network_transmit_bytes,
                    AVG(disk_read_bytes)          AS disk_read_bytes,
                    AVG(disk_write_bytes)         AS disk_write_bytes,
                    MAX(storage_request_bytes)    AS storage_request_bytes,
                    MAX(storage_usage_bytes)      AS storage_usage_bytes,
                    MAX(gpu_usage_millicores)     AS gpu_usage_millicores,
                    MAX(restart_count)            AS restart_count,
                    owner_kind,
                    owner_name,
                    SUM(COALESCE(duration_seconds, 0)) AS duration_seconds,
                    node,
                    node_instance_type,
                    node_zone,
                    emaps_zone,
                    MAX(COALESCE(is_estimated, 0)) AS is_estimated,
                    '[]'                          AS estimation_reasons,
                    MAX(calculation_version)      AS calculation_version
                FROM combined_metrics
                WHERE "timestamp" < ?
                GROUP BY pod_name, namespace, strftime('%Y-%m-%dT%H:00:00Z', "timestamp")
                """,
                (cutoff_iso,),
            )
            count = cursor.rowcount
            await conn.commit()
            return {"hours": count, "rows": count}

    # ------------------------------------------------------------------
    # Retention: prune old raw rows
    # ------------------------------------------------------------------

    async def _prune_raw(self) -> int:
        """Delete raw metrics older than METRICS_RAW_RETENTION_DAYS.

        Set to -1 to disable pruning (keep raw data indefinitely).
        """
        retention_days = self._config.METRICS_RAW_RETENTION_DAYS
        if retention_days < 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        if self._db.db_type == "postgres":
            async with self._db.connection_scope() as conn:
                result = await conn.execute(
                    "DELETE FROM combined_metrics WHERE timestamp < $1",
                    cutoff,
                )
                count = int(result.split()[-1]) if result else 0
                logger.info("Pruned %d raw metrics older than %d days", count, retention_days)
                return count
        else:
            async with self._db.connection_scope() as conn:
                cursor = await conn.execute(
                    'DELETE FROM combined_metrics WHERE "timestamp" < ?',
                    (cutoff.isoformat(),),
                )
                count = cursor.rowcount
                await conn.commit()
                logger.info("Pruned %d raw metrics older than %d days", count, retention_days)
                return count

    async def _prune_hourly(self) -> int:
        """Delete hourly aggregates older than METRICS_AGGREGATED_RETENTION_DAYS.

        Set to -1 to disable pruning (keep aggregated data indefinitely).
        """
        retention_days = self._config.METRICS_AGGREGATED_RETENTION_DAYS
        if retention_days < 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        if self._db.db_type == "postgres":
            async with self._db.connection_scope() as conn:
                result = await conn.execute(
                    "DELETE FROM combined_metrics_hourly WHERE hour_bucket < $1",
                    cutoff,
                )
                count = int(result.split()[-1]) if result else 0
                logger.info("Pruned %d hourly metrics older than %d days", count, retention_days)
                return count
        else:
            async with self._db.connection_scope() as conn:
                cursor = await conn.execute(
                    "DELETE FROM combined_metrics_hourly WHERE hour_bucket < ?",
                    (cutoff.isoformat(),),
                )
                count = cursor.rowcount
                await conn.commit()
                logger.info("Pruned %d hourly metrics older than %d days", count, retention_days)
                return count

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
                count = int(result.split()[-1]) if result else 0
                return count
        else:
            async with self._db.connection_scope() as conn:
                cursor = await conn.execute(
                    """
                    INSERT OR REPLACE INTO namespace_cache (namespace, last_seen)
                    SELECT DISTINCT namespace, datetime('now')
                    FROM combined_metrics
                    WHERE "timestamp" > datetime('now', '-7 days')
                    """
                )
                count = cursor.rowcount
                await conn.commit()
                return count
