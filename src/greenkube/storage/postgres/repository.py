import json
import logging
from datetime import datetime
from typing import List, Optional

from ...core.exceptions import QueryError
from ...models.metrics import CombinedMetric
from ..base_repository import CarbonIntensityRepository, CombinedMetricsRepository

logger = logging.getLogger(__name__)


class PostgresCarbonIntensityRepository(CarbonIntensityRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> Optional[float]:
        try:
            async with self.db_manager.connection_scope() as conn:
                # Parse timestamp string to datetime object if needed
                if isinstance(timestamp, datetime):
                    ts = timestamp
                else:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

                # Use $n placeholders for asyncpg
                query = """
                    SELECT carbon_intensity
                    FROM carbon_intensity_history
                    WHERE zone = $1 AND datetime <= $2
                    ORDER BY datetime DESC
                    LIMIT 1
                """
                row = await conn.fetchrow(query, zone, ts)
                if row:
                    return row["carbon_intensity"]
                return None
        except Exception as e:
            logger.error("Error getting carbon intensity from Postgres: %s", e)
            raise QueryError(f"Error getting carbon intensity: {e}") from e

    async def save_history(self, history_data: list, zone: str) -> int:
        if not history_data:
            return 0

        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    INSERT INTO carbon_intensity_history (
                        zone, carbon_intensity, datetime, updated_at, created_at,
                        emission_factor_type, is_estimated, estimation_method
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8
                    )
                    ON CONFLICT (zone, datetime) DO UPDATE SET
                        carbon_intensity = EXCLUDED.carbon_intensity,
                        updated_at = EXCLUDED.updated_at,
                        emission_factor_type = EXCLUDED.emission_factor_type;
                """

                records = []
                for record in history_data:
                    # Parse timestamp if string, handling Z suffix
                    ts = record.get("datetime")
                    if ts and isinstance(ts, str):
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))

                    # Parse updated_at
                    updated_at = record.get("updatedAt")
                    if updated_at and isinstance(updated_at, str):
                        updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

                    # Parse created_at
                    created_at = record.get("createdAt")
                    if created_at and isinstance(created_at, str):
                        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

                    records.append(
                        (
                            zone,
                            record.get("carbonIntensity"),
                            ts,
                            updated_at,
                            created_at,
                            record.get("emissionFactorType"),
                            record.get("isEstimated"),
                            record.get("estimationMethod"),
                        )
                    )

                await conn.executemany(query, records)
                logger.info("Saved %s records to Postgres for zone %s.", len(history_data), zone)
                return len(history_data)
        except Exception as e:
            logger.error("Error saving history to Postgres: %s", e)
            raise QueryError(f"Error saving history: {e}") from e


class PostgresCombinedMetricsRepository(CombinedMetricsRepository):
    """PostgreSQL implementation for combined metrics data storage."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def write_combined_metrics(self, metrics: List[CombinedMetric]) -> int:
        if not metrics:
            return 0

        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    INSERT INTO combined_metrics (
                        pod_name, namespace, total_cost, co2e_grams,
                        pue, grid_intensity, joules, cpu_request,
                        memory_request, cpu_usage_millicores, memory_usage_bytes,
                        network_receive_bytes, network_transmit_bytes,
                        disk_read_bytes, disk_write_bytes,
                        storage_request_bytes, storage_usage_bytes,
                        ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                        gpu_usage_millicores, restart_count,
                        owner_kind, owner_name,
                        period, timestamp,
                        duration_seconds, grid_intensity_timestamp,
                        node, node_instance_type, node_zone,
                        emaps_zone, estimation_reasons, is_estimated, embodied_co2e_grams,
                        calculation_version
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11,
                        $12, $13,
                        $14, $15,
                        $16, $17,
                        $18, $19,
                        $20, $21,
                        $22, $23,
                        $24, $25,
                        $26, $27,
                        $28, $29, $30,
                        $31, $32, $33, $34,
                        $35
                    )
                    ON CONFLICT (pod_name, namespace, timestamp) DO UPDATE SET
                        total_cost = EXCLUDED.total_cost,
                        co2e_grams = EXCLUDED.co2e_grams,
                        pue = EXCLUDED.pue,
                        grid_intensity = EXCLUDED.grid_intensity,
                        joules = EXCLUDED.joules,
                        cpu_request = EXCLUDED.cpu_request,
                        memory_request = EXCLUDED.memory_request,
                        cpu_usage_millicores = EXCLUDED.cpu_usage_millicores,
                        memory_usage_bytes = EXCLUDED.memory_usage_bytes,
                        network_receive_bytes = EXCLUDED.network_receive_bytes,
                        network_transmit_bytes = EXCLUDED.network_transmit_bytes,
                        disk_read_bytes = EXCLUDED.disk_read_bytes,
                        disk_write_bytes = EXCLUDED.disk_write_bytes,
                        storage_request_bytes = EXCLUDED.storage_request_bytes,
                        storage_usage_bytes = EXCLUDED.storage_usage_bytes,
                        ephemeral_storage_request_bytes = EXCLUDED.ephemeral_storage_request_bytes,
                        ephemeral_storage_usage_bytes = EXCLUDED.ephemeral_storage_usage_bytes,
                        gpu_usage_millicores = EXCLUDED.gpu_usage_millicores,
                        restart_count = EXCLUDED.restart_count,
                        owner_kind = EXCLUDED.owner_kind,
                        owner_name = EXCLUDED.owner_name,
                        period = EXCLUDED.period,
                        duration_seconds = EXCLUDED.duration_seconds,
                        grid_intensity_timestamp = EXCLUDED.grid_intensity_timestamp,
                        node = EXCLUDED.node,
                        node_instance_type = EXCLUDED.node_instance_type,
                        node_zone = EXCLUDED.node_zone,
                        emaps_zone = EXCLUDED.emaps_zone,
                        estimation_reasons = EXCLUDED.estimation_reasons,
                        is_estimated = EXCLUDED.is_estimated,
                        embodied_co2e_grams = EXCLUDED.embodied_co2e_grams,
                        calculation_version = EXCLUDED.calculation_version;
                """

                metrics_data = []
                for metric in metrics:
                    reasons = metric.estimation_reasons
                    reasons_json = json.dumps(reasons) if reasons else "[]"

                    metrics_data.append(
                        (
                            metric.pod_name,
                            metric.namespace,
                            metric.total_cost,
                            metric.co2e_grams,
                            metric.pue,
                            metric.grid_intensity,
                            metric.joules,
                            metric.cpu_request,
                            metric.memory_request,
                            metric.cpu_usage_millicores,
                            metric.memory_usage_bytes,
                            metric.network_receive_bytes,
                            metric.network_transmit_bytes,
                            metric.disk_read_bytes,
                            metric.disk_write_bytes,
                            metric.storage_request_bytes,
                            metric.storage_usage_bytes,
                            metric.ephemeral_storage_request_bytes,
                            metric.ephemeral_storage_usage_bytes,
                            metric.gpu_usage_millicores,
                            metric.restart_count,
                            metric.owner_kind,
                            metric.owner_name,
                            metric.period,
                            metric.timestamp,
                            metric.duration_seconds,
                            metric.grid_intensity_timestamp,
                            metric.node,
                            metric.node_instance_type,
                            metric.node_zone,
                            metric.emaps_zone,
                            reasons_json,
                            metric.is_estimated,
                            metric.embodied_co2e_grams,
                            metric.calculation_version,
                        )
                    )

                await conn.executemany(query, metrics_data)
                # No commit needed as asyncpg usually autocommits or we rely on explicit transaction
                logger.info("Saved %s combined metrics to Postgres.", len(metrics))
                return len(metrics)
        except Exception as e:
            logger.error("Error writing combined metrics to Postgres: %s", e)
            raise QueryError(f"Error writing combined metrics: {e}") from e

    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        """
        Reads combined metrics from the database within a time range.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    SELECT * FROM combined_metrics
                    WHERE timestamp >= $1 AND timestamp <= $2
                    ORDER BY timestamp
                """
                results = await conn.fetch(query, start_time, end_time)

                metrics = []
                for row in results:
                    metric_data = dict(row)

                    # Remove database-only columns not in CombinedMetric model
                    metric_data.pop("id", None)

                    # Deserialize estimation_reasons from JSON string
                    if "estimation_reasons" in metric_data and isinstance(metric_data["estimation_reasons"], str):
                        try:
                            metric_data["estimation_reasons"] = json.loads(metric_data["estimation_reasons"])
                        except json.JSONDecodeError:
                            metric_data["estimation_reasons"] = []

                    metrics.append(CombinedMetric(**metric_data))

                return metrics
        except Exception as e:
            logger.error("Error reading combined metrics from Postgres: %s", e)
            raise QueryError(f"Error reading combined metrics: {e}") from e

    async def read_hourly_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        """Read pre-aggregated hourly metrics from the hourly table."""
        try:
            async with self.db_manager.connection_scope() as conn:
                if namespace:
                    query = """
                        SELECT * FROM combined_metrics_hourly
                        WHERE hour_bucket >= $1 AND hour_bucket <= $2
                          AND namespace = $3
                        ORDER BY hour_bucket
                    """
                    results = await conn.fetch(query, start_time, end_time, namespace)
                else:
                    query = """
                        SELECT * FROM combined_metrics_hourly
                        WHERE hour_bucket >= $1 AND hour_bucket <= $2
                        ORDER BY hour_bucket
                    """
                    results = await conn.fetch(query, start_time, end_time)

                metrics = []
                for row in results:
                    data = dict(row)
                    data.pop("id", None)
                    # Map hourly columns back to CombinedMetric fields
                    data["timestamp"] = data.pop("hour_bucket", None)
                    data["cpu_usage_millicores"] = data.pop("cpu_usage_avg", None)
                    data["cpu_usage_max_millicores"] = data.pop("cpu_usage_max", None)
                    data["memory_usage_bytes"] = data.pop("memory_usage_avg", None)
                    data["memory_usage_max_bytes"] = data.pop("memory_usage_max", None)
                    if "estimation_reasons" in data and isinstance(data["estimation_reasons"], str):
                        try:
                            data["estimation_reasons"] = json.loads(data["estimation_reasons"])
                        except json.JSONDecodeError:
                            data["estimation_reasons"] = []
                    metrics.append(CombinedMetric(**data))
                return metrics
        except Exception as e:
            logger.error("Error reading hourly metrics from Postgres: %s", e)
            raise QueryError(f"Error reading hourly metrics: {e}") from e

    async def list_metric_years(self, namespace: Optional[str] = None) -> List[int]:
        """Return distinct metric years from raw and hourly Postgres tables."""
        try:
            async with self.db_manager.connection_scope() as conn:
                if namespace:
                    query = """
                        SELECT DISTINCT year FROM (
                            SELECT EXTRACT(YEAR FROM timestamp)::int AS year
                            FROM combined_metrics
                            WHERE timestamp IS NOT NULL AND namespace = $1
                            UNION
                            SELECT EXTRACT(YEAR FROM hour_bucket)::int AS year
                            FROM combined_metrics_hourly
                            WHERE hour_bucket IS NOT NULL AND namespace = $1
                        ) AS metric_years
                        WHERE year IS NOT NULL
                        ORDER BY year DESC
                    """
                    rows = await conn.fetch(query, namespace)
                else:
                    query = """
                        SELECT DISTINCT year FROM (
                            SELECT EXTRACT(YEAR FROM timestamp)::int AS year
                            FROM combined_metrics
                            WHERE timestamp IS NOT NULL
                            UNION
                            SELECT EXTRACT(YEAR FROM hour_bucket)::int AS year
                            FROM combined_metrics_hourly
                            WHERE hour_bucket IS NOT NULL
                        ) AS metric_years
                        WHERE year IS NOT NULL
                        ORDER BY year DESC
                    """
                    rows = await conn.fetch(query)
                return [int(row["year"]) for row in rows]
        except Exception as e:
            logger.error("Error listing metric years from Postgres: %s", e)
            raise QueryError(f"Error listing metric years: {e}") from e

    async def aggregate_summary(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> dict:
        """SQL-level aggregation for summary — queries both raw and hourly tables."""
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                # Raw table — only for data within the compression window
                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT co2e_grams, embodied_co2e_grams, total_cost, joules,
                               pod_name, namespace
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([raw_start, end_time])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                # Hourly table — only for data before the compression cutoff
                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT co2e_grams, embodied_co2e_grams, total_cost, joules,
                               pod_name, namespace
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([start_time, hourly_end])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return {
                        "total_co2e_grams": 0.0,
                        "total_embodied_co2e_grams": 0.0,
                        "total_cost": 0.0,
                        "total_energy_joules": 0.0,
                        "pod_count": 0,
                        "namespace_count": 0,
                        "row_count": 0,
                    }

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        COALESCE(SUM(co2e_grams), 0)          AS total_co2e,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS total_embodied,
                        COALESCE(SUM(total_cost), 0)          AS total_cost,
                        COALESCE(SUM(joules), 0)              AS total_energy,
                        COUNT(DISTINCT pod_name)               AS pod_count,
                        COUNT(DISTINCT namespace)              AS namespace_count,
                        COUNT(*)                               AS row_count
                    FROM ({union_query}) AS combined
                """
                row = await conn.fetchrow(query, *params)

                return {
                    "total_co2e_grams": row["total_co2e"] or 0.0,
                    "total_embodied_co2e_grams": row["total_embodied"] or 0.0,
                    "total_cost": row["total_cost"] or 0.0,
                    "total_energy_joules": row["total_energy"] or 0.0,
                    "pod_count": row["pod_count"] or 0,
                    "namespace_count": row["namespace_count"] or 0,
                    "row_count": row["row_count"] or 0,
                }
        except Exception as e:
            logger.error("Error in aggregate_summary: %s", e)
            raise QueryError(f"aggregate_summary failed: {e}") from e

    async def aggregate_grouped_row_count(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        granularity: Optional[str] = None,
        group_by: str = "pod",
    ) -> int:
        """SQL COUNT of distinct groups for a grouped report — no rows loaded.

        Computes COUNT(DISTINCT (group_key, time_bucket)) in the DB so the
        caller never materialises any CombinedMetric objects.

        Handles the raw/hourly split the same way as aggregate_summary.
        """
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        # Map report granularity names → PostgreSQL date_trunc units
        _GRAN_TRUNC = {
            "hourly": "hour",
            "daily": "day",
            "weekly": "week",
            "monthly": "month",
            "yearly": "year",
        }

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)
        start_aware = start_time if start_time.tzinfo else start_time.replace(tzinfo=tz.utc)
        end_aware = end_time if end_time.tzinfo else end_time.replace(tzinfo=tz.utc)

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                # Build the group expression common to both tables
                if group_by == "namespace":
                    group_cols = "namespace"
                else:
                    group_cols = "namespace, pod_name"

                trunc_unit = _GRAN_TRUNC.get(granularity or "", None)
                if trunc_unit:
                    # Group by identity cols + time bucket
                    raw_ts_expr = f"date_trunc('{trunc_unit}', timestamp)"
                    hourly_ts_expr = f"date_trunc('{trunc_unit}', hour_bucket)"
                else:
                    raw_ts_expr = "NULL::timestamptz"
                    hourly_ts_expr = "NULL::timestamptz"

                if end_aware >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_aware, cutoff - timedelta(minutes=1))
                    ns_clause = f" AND namespace = ${idx + 2}" if namespace else ""
                    parts.append(f"""
                        SELECT {group_cols}, {raw_ts_expr} AS ts_bucket
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([raw_start, end_time])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if start_aware < cutoff:
                    hourly_end = min(end_aware, cutoff)
                    ns_clause = f" AND namespace = ${idx + 2}" if namespace else ""
                    parts.append(f"""
                        SELECT {group_cols}, {hourly_ts_expr} AS ts_bucket
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([start_time, hourly_end])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return 0

                union_cte = " UNION ALL ".join(parts)
                if group_by == "namespace":
                    distinct_cols = "namespace, ts_bucket"
                else:
                    distinct_cols = "namespace, pod_name, ts_bucket"

                query = f"""
                    SELECT COUNT(*) AS group_count
                    FROM (
                        SELECT DISTINCT {distinct_cols}
                        FROM ({union_cte}) AS all_rows
                    ) AS groups
                """
                row = await conn.fetchrow(query, *params)
                return int(row["group_count"]) if row else 0
        except Exception as e:
            logger.error("Error in aggregate_grouped_row_count: %s", e)
            raise QueryError(f"aggregate_grouped_row_count failed: {e}") from e

    async def aggregate_timeseries(
        self,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "hour",
        namespace: Optional[str] = None,
    ) -> list:
        """SQL-level time-series aggregation — queries both raw and hourly tables."""
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        _PG_TRUNC = {
            "hour": "hour",
            "day": "day",
            "week": "week",
            "month": "month",
        }
        trunc_unit = _PG_TRUNC.get(granularity, "hour")

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                # Raw table — recent data
                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT timestamp AS ts, co2e_grams, embodied_co2e_grams,
                               total_cost, joules, cpu_usage_millicores, memory_usage_bytes
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([raw_start, end_time])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                # Hourly table — old data
                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT hour_bucket AS ts, co2e_grams, embodied_co2e_grams,
                               total_cost, joules, cpu_usage_avg AS cpu_usage_millicores,
                               memory_usage_avg AS memory_usage_bytes
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([start_time, hourly_end])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return []

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        date_trunc('{trunc_unit}', ts) AS ts_bucket,
                        COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                        COALESCE(SUM(total_cost), 0)          AS total_cost,
                        COALESCE(SUM(joules), 0)              AS energy_joules,
                        COALESCE(SUM(cpu_usage_millicores), 0) AS cpu_usage_millicores,
                        COALESCE(SUM(memory_usage_bytes), 0)  AS memory_usage_bytes
                    FROM ({union_query}) AS combined
                    GROUP BY ts_bucket
                    ORDER BY ts_bucket
                """
                rows = await conn.fetch(query, *params)

                _FORMATS = {
                    "hour": "%Y-%m-%dT%H:00:00Z",
                    "day": "%Y-%m-%dT00:00:00Z",
                    "week": "%Y-%m-%dT00:00:00Z",  # date_trunc('week') → Monday
                    "month": "%Y-%m-01T00:00:00Z",
                }
                fmt = _FORMATS.get(granularity, "%Y-%m-%dT%H:00:00Z")
                return [
                    {
                        "timestamp": row["ts_bucket"].strftime(fmt) if row["ts_bucket"] else "",
                        "co2e_grams": row["co2e_grams"],
                        "embodied_co2e_grams": row["embodied_co2e_grams"],
                        "total_cost": row["total_cost"],
                        "energy_joules": row["energy_joules"],
                        "cpu_usage_millicores": row["cpu_usage_millicores"],
                        "memory_usage_bytes": row["memory_usage_bytes"],
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Error in aggregate_timeseries: %s", e)
            raise QueryError(f"aggregate_timeseries failed: {e}") from e

    async def list_namespaces(self) -> list:
        """Return namespaces from the cache table (fast, no table scan)."""
        try:
            async with self.db_manager.connection_scope() as conn:
                rows = await conn.fetch("SELECT namespace FROM namespace_cache ORDER BY namespace")
                if rows:
                    return [row["namespace"] for row in rows]
                # Fallback: scan recent combined_metrics if cache is empty
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT namespace
                    FROM combined_metrics
                    WHERE timestamp > NOW() - INTERVAL '7 days'
                    ORDER BY namespace
                    """
                )
                return [row["namespace"] for row in rows]
        except Exception as e:
            logger.warning("list_namespaces failed, returning empty list: %s", e)
            return []

    async def aggregate_by_namespace(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> list:
        """SQL-level aggregation of metrics grouped by namespace."""
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT namespace, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([raw_start, end_time])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT namespace, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([start_time, hourly_end])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return []

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        namespace,
                        COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                        COALESCE(SUM(total_cost), 0)           AS total_cost,
                        COALESCE(SUM(joules), 0)               AS energy_joules
                    FROM ({union_query}) AS combined
                    GROUP BY namespace
                    ORDER BY co2e_grams DESC
                """
                rows = await conn.fetch(query, *params)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Error in aggregate_by_namespace: %s", e)
            raise QueryError(f"aggregate_by_namespace failed: {e}") from e

    async def aggregate_top_pods(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        limit: int = 10,
    ) -> list:
        """SQL-level aggregation of top pods by CO2 emissions."""
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT namespace, pod_name, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([raw_start, end_time])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT namespace, pod_name, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([start_time, hourly_end])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return []

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        namespace,
                        pod_name,
                        COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                        COALESCE(SUM(total_cost), 0)           AS total_cost,
                        COALESCE(SUM(joules), 0)               AS energy_joules
                    FROM ({union_query}) AS combined
                    GROUP BY namespace, pod_name
                    ORDER BY co2e_grams DESC
                    LIMIT ${idx}
                """
                params.append(limit)
                rows = await conn.fetch(query, *params)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Error in aggregate_top_pods: %s", e)
            raise QueryError(f"aggregate_top_pods failed: {e}") from e

    async def read_combined_metrics_page(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> tuple[int, List[CombinedMetric]]:
        """DB-level paginated read: COUNT(total) + LIMIT/OFFSET in a single round-trip.

        Handles the raw/hourly split:
        - Recent data (within METRICS_COMPRESSION_AGE_HOURS): queries combined_metrics.
        - Older data: queries combined_metrics_hourly.
        - Mixed ranges: UNION ALL with column normalization.

        Returns:
            Tuple of (total_count, page_items).
        """
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)
        start_aware = start_time if start_time.tzinfo else start_time.replace(tzinfo=tz.utc)
        end_aware = end_time if end_time.tzinfo else end_time.replace(tzinfo=tz.utc)

        try:
            async with self.db_manager.connection_scope() as conn:
                # --- Build UNION ALL parts with normalized columns ---
                # Both tables expose the same set of fields; the hourly table
                # renames a few columns (hour_bucket → timestamp, cpu/mem avg → usage).
                parts: list[str] = []
                params: list = []
                idx = 1

                if end_aware >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_aware, cutoff - timedelta(minutes=1))
                    ns_clause = f" AND namespace = ${idx + 2}" if namespace else ""
                    parts.append(f"""
                        SELECT
                            pod_name, namespace, total_cost, co2e_grams, pue,
                            grid_intensity, joules, cpu_request, memory_request,
                            cpu_usage_millicores, memory_usage_bytes,
                            network_receive_bytes, network_transmit_bytes,
                            disk_read_bytes, disk_write_bytes,
                            storage_request_bytes, storage_usage_bytes,
                            ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                            gpu_usage_millicores, restart_count,
                            owner_kind, owner_name, period, timestamp,
                            duration_seconds, grid_intensity_timestamp,
                            node, node_instance_type, node_zone,
                            emaps_zone, estimation_reasons, is_estimated,
                            embodied_co2e_grams, calculation_version
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([raw_start, end_time])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if start_aware < cutoff:
                    hourly_end = min(end_aware, cutoff)
                    ns_clause = f" AND namespace = ${idx + 2}" if namespace else ""
                    parts.append(f"""
                        SELECT
                            pod_name, namespace, total_cost, co2e_grams, pue,
                            grid_intensity, joules, cpu_request, memory_request,
                            cpu_usage_avg AS cpu_usage_millicores,
                            memory_usage_avg AS memory_usage_bytes,
                            network_receive_bytes, network_transmit_bytes,
                            disk_read_bytes, disk_write_bytes,
                            storage_request_bytes, storage_usage_bytes,
                            ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                            gpu_usage_millicores, restart_count,
                            owner_kind, owner_name, period, hour_bucket AS timestamp,
                            duration_seconds, grid_intensity_timestamp,
                            node, node_instance_type, node_zone,
                            emaps_zone, estimation_reasons, is_estimated,
                            embodied_co2e_grams, calculation_version
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket <= ${idx + 1}{ns_clause}
                    """)
                    params.extend([start_time, hourly_end])
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return 0, []

                union_cte = " UNION ALL ".join(parts)

                # COUNT and paged SELECT in a single CTE to avoid two round-trips.
                count_query = f"SELECT COUNT(*) AS cnt FROM ({union_cte}) AS all_rows"
                total_row = await conn.fetchrow(count_query, *params)
                total = int(total_row["cnt"]) if total_row else 0

                if total == 0 or offset >= total:
                    return total, []

                page_query = f"""
                    SELECT * FROM ({union_cte}) AS all_rows
                    ORDER BY timestamp
                    LIMIT ${idx} OFFSET ${idx + 1}
                """
                params.extend([limit, offset])
                rows = await conn.fetch(page_query, *params)

                metrics = []
                for row in rows:
                    data = dict(row)
                    if "estimation_reasons" in data and isinstance(data["estimation_reasons"], str):
                        try:
                            import json as _json

                            data["estimation_reasons"] = _json.loads(data["estimation_reasons"])
                        except Exception:
                            data["estimation_reasons"] = []
                    metrics.append(CombinedMetric(**data))

                return total, metrics
        except Exception as e:
            logger.error("Error in read_combined_metrics_page: %s", e)
            raise QueryError(f"read_combined_metrics_page failed: {e}") from e

    async def read_latest_per_pod(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> List[CombinedMetric]:
        """Return the single most-recent metric snapshot for each (namespace, pod_name).

        Uses PostgreSQL DISTINCT ON to avoid loading full history into memory.
        This replaces the pattern of reading all rows and deduplicating in Python.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    SELECT DISTINCT ON (namespace, pod_name) *
                    FROM combined_metrics
                    WHERE timestamp >= $1 AND timestamp <= $2
                    ORDER BY namespace, pod_name, timestamp DESC
                """
                rows = await conn.fetch(query, start_time, end_time)
                metrics = []
                for row in rows:
                    import json as _json

                    data = dict(row)
                    data.pop("id", None)
                    if "estimation_reasons" in data and isinstance(data["estimation_reasons"], str):
                        try:
                            data["estimation_reasons"] = _json.loads(data["estimation_reasons"])
                        except Exception:
                            data["estimation_reasons"] = []
                    metrics.append(CombinedMetric(**data))
                return metrics
        except Exception as e:
            logger.error("Error in read_latest_per_pod: %s", e)
            raise QueryError(f"read_latest_per_pod failed: {e}") from e
