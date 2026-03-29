import json
import logging
import sqlite3
from datetime import datetime
from typing import List, Optional

import aiosqlite

from greenkube.models.metrics import CombinedMetric
from greenkube.utils.date_utils import ensure_utc, to_iso_z

from ..core.exceptions import QueryError
from .base_repository import CarbonIntensityRepository, CombinedMetricsRepository

logger = logging.getLogger(__name__)


class SQLiteCarbonIntensityRepository(CarbonIntensityRepository):
    """
    SQLite implementation for carbon intensity data storage.
    Handles saving and retrieving carbon intensity history records.
    """

    def __init__(self, db_manager):
        """
        Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.
        Consistent with the PostgreSQL implementation (no arbitrary lookback window).
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row

                # Normalize the query timestamp to ensure it matches the stored format (Z suffix)
                try:
                    dt = ensure_utc(timestamp)
                    normalized_ts = to_iso_z(dt)
                except ValueError:
                    normalized_ts = timestamp

                query = """
                    SELECT carbon_intensity
                    FROM carbon_intensity_history
                    WHERE zone = ? AND datetime <= ?
                    ORDER BY datetime DESC
                    LIMIT 1
                """
                params = (zone, normalized_ts)

                async with conn.execute(query, params) as cursor:
                    result = await cursor.fetchone()
                    return result["carbon_intensity"] if result else None
        except sqlite3.Error as e:
            logger.error("Database error in get_for_zone_at_time for zone %s at %s: %s", zone, timestamp, e)
            raise QueryError(f"Database error in get_for_zone_at_time: {e}") from e
        except Exception as e:
            logger.error("Unexpected error in get_for_zone_at_time: %s", e)
            raise QueryError(f"Unexpected error in get_for_zone_at_time: {e}") from e

    async def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves historical carbon intensity data to the SQLite database.
        It ignores records that would be duplicates based on zone and datetime.
        """
        saved_count = 0

        try:
            async with self.db_manager.connection_scope() as conn:
                for record in history_data:
                    # Basic validation that record is a dictionary
                    if not isinstance(record, dict):
                        logging.warning("Skipping invalid record (not a dict): %s", record)
                        continue

                    try:
                        # Normalize datetime
                        raw_dt = record.get("datetime")
                        if raw_dt:
                            normalized_dt = to_iso_z(ensure_utc(raw_dt))
                        else:
                            normalized_dt = None

                        # Use default value None if key is missing
                        cursor = await conn.execute(
                            """
                            INSERT INTO carbon_intensity_history
                                (zone, carbon_intensity, datetime, updated_at, created_at,
                                 emission_factor_type, is_estimated, estimation_method)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(zone, datetime)
                            DO UPDATE SET
                                carbon_intensity = excluded.carbon_intensity,
                                updated_at = excluded.updated_at,
                                is_estimated = excluded.is_estimated,
                                estimation_method = excluded.estimation_method,
                                emission_factor_type = excluded.emission_factor_type;
                        """,
                            (
                                zone,
                                record.get("carbonIntensity"),
                                normalized_dt,
                                record.get("updatedAt"),
                                record.get("createdAt"),
                                record.get("emissionFactorType"),
                                record.get("isEstimated"),
                                record.get("estimationMethod"),
                            ),
                        )
                        # cursor.rowcount will be 1 for a successful insert, 0 for conflict/no insert
                        # actually for ON CONFLICT DO UPDATE it might be 1?
                        saved_count += cursor.rowcount
                    except sqlite3.Error as e:
                        # Use logging for errors
                        logging.error("Could not save record for zone %s at %s: %s", zone, record.get("datetime"), e)
                    except Exception as e:
                        # Catch potential errors from record.get() if record structure is unexpected
                        logging.error("Unexpected error processing record %s: %s", record, e)

                await conn.commit()
                return saved_count
        except sqlite3.Error as e:
            logging.error("Failed to commit transaction: %s", e)
            raise QueryError(f"Failed to commit transaction: {e}") from e
        except Exception as e:
            logging.error("Unexpected error in save_history: %s", e)
            raise QueryError(f"Unexpected error in save_history: {e}") from e


class SQLiteCombinedMetricsRepository(CombinedMetricsRepository):
    """
    SQLite implementation for combined metrics data storage.
    Handles writing and reading CombinedMetric records.
    """

    def __init__(self, db_manager):
        """
        Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def write_combined_metrics(self, metrics: List[CombinedMetric]) -> int:
        saved_count = 0
        try:
            async with self.db_manager.connection_scope() as conn:
                for metric in metrics:
                    try:
                        timestamp_iso = metric.timestamp.isoformat() if metric.timestamp else None
                        grid_intensity_timestamp_iso = (
                            metric.grid_intensity_timestamp.isoformat() if metric.grid_intensity_timestamp else None
                        )

                        cursor = await conn.execute(
                            """
                            INSERT INTO combined_metrics
                                 (pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                                  cpu_request, memory_request, cpu_usage_millicores, memory_usage_bytes,
                                  network_receive_bytes, network_transmit_bytes,
                                  disk_read_bytes, disk_write_bytes,
                                  storage_request_bytes, storage_usage_bytes,
                                  ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                                  gpu_usage_millicores, restart_count,
                                  owner_kind, owner_name,
                                  period, "timestamp", duration_seconds,
                                  grid_intensity_timestamp, node, node_instance_type, node_zone, emaps_zone,
                                  is_estimated, estimation_reasons, embodied_co2e_grams,
                                  calculation_version)
                            VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?
                            )
                            ON CONFLICT(pod_name, namespace, "timestamp") DO UPDATE SET
                                total_cost = excluded.total_cost,
                                co2e_grams = excluded.co2e_grams,
                                pue = excluded.pue,
                                grid_intensity = excluded.grid_intensity,
                                joules = excluded.joules,
                                cpu_request = excluded.cpu_request,
                                memory_request = excluded.memory_request,
                                cpu_usage_millicores = excluded.cpu_usage_millicores,
                                memory_usage_bytes = excluded.memory_usage_bytes,
                                network_receive_bytes = excluded.network_receive_bytes,
                                network_transmit_bytes = excluded.network_transmit_bytes,
                                disk_read_bytes = excluded.disk_read_bytes,
                                disk_write_bytes = excluded.disk_write_bytes,
                                storage_request_bytes = excluded.storage_request_bytes,
                                storage_usage_bytes = excluded.storage_usage_bytes,
                                ephemeral_storage_request_bytes = excluded.ephemeral_storage_request_bytes,
                                ephemeral_storage_usage_bytes = excluded.ephemeral_storage_usage_bytes,
                                gpu_usage_millicores = excluded.gpu_usage_millicores,
                                restart_count = excluded.restart_count,
                                owner_kind = excluded.owner_kind,
                                owner_name = excluded.owner_name,
                                period = excluded.period,
                                duration_seconds = excluded.duration_seconds,
                                grid_intensity_timestamp = excluded.grid_intensity_timestamp,
                                node = excluded.node,
                                node_instance_type = excluded.node_instance_type,
                                node_zone = excluded.node_zone,
                                emaps_zone = excluded.emaps_zone,
                                is_estimated = excluded.is_estimated,
                                estimation_reasons = excluded.estimation_reasons,
                                embodied_co2e_grams = excluded.embodied_co2e_grams,
                                calculation_version = excluded.calculation_version;
                        """,
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
                                timestamp_iso,
                                metric.duration_seconds,
                                grid_intensity_timestamp_iso,
                                metric.node,
                                metric.node_instance_type,
                                metric.node_zone,
                                metric.emaps_zone,
                                metric.is_estimated,
                                json.dumps(metric.estimation_reasons) if metric.estimation_reasons else "[]",
                                metric.embodied_co2e_grams,
                                metric.calculation_version,
                            ),
                        )
                        saved_count += cursor.rowcount
                    except sqlite3.Error as e:
                        logging.error("Could not save combined metric for pod %s: %s", metric.pod_name, e)
                    except Exception as e:
                        logging.error("Unexpected error processing combined metric %s: %s", metric.pod_name, e)

                await conn.commit()
                return saved_count
        except sqlite3.Error as e:
            logging.error("Failed to commit transaction for combined_metrics: %s", e)
            raise QueryError(f"Failed to commit combined_metrics: {e}") from e
        except Exception as e:
            logging.error("Unexpected error in write_combined_metrics: %s", e)
            raise QueryError(f"Unexpected error in write_combined_metrics: {e}") from e

    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    """
                    SELECT pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                           cpu_request, memory_request, cpu_usage_millicores, memory_usage_bytes,
                           network_receive_bytes, network_transmit_bytes,
                           disk_read_bytes, disk_write_bytes,
                           storage_request_bytes, storage_usage_bytes,
                           ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                           gpu_usage_millicores, restart_count,
                           owner_kind, owner_name,
                           period, "timestamp", duration_seconds,
                           grid_intensity_timestamp, node, node_instance_type, node_zone, emaps_zone,
                           is_estimated, estimation_reasons, embodied_co2e_grams,
                           calculation_version
                    FROM combined_metrics
                    WHERE "timestamp" BETWEEN ? AND ?
                """,
                    (start_time.isoformat(), end_time.isoformat()),
                ) as cursor:
                    rows = await cursor.fetchall()
                    metrics = []
                    for row in rows:
                        estimation_reasons = []
                        if row["estimation_reasons"]:
                            try:
                                estimation_reasons = json.loads(row["estimation_reasons"])
                            except json.JSONDecodeError:
                                pass

                        metrics.append(
                            CombinedMetric(
                                pod_name=row["pod_name"],
                                namespace=row["namespace"],
                                total_cost=row["total_cost"],
                                co2e_grams=row["co2e_grams"],
                                pue=row["pue"],
                                grid_intensity=row["grid_intensity"],
                                joules=row["joules"],
                                cpu_request=row["cpu_request"],
                                memory_request=row["memory_request"],
                                cpu_usage_millicores=row["cpu_usage_millicores"],
                                memory_usage_bytes=row["memory_usage_bytes"],
                                network_receive_bytes=row["network_receive_bytes"],
                                network_transmit_bytes=row["network_transmit_bytes"],
                                disk_read_bytes=row["disk_read_bytes"],
                                disk_write_bytes=row["disk_write_bytes"],
                                storage_request_bytes=row["storage_request_bytes"],
                                storage_usage_bytes=row["storage_usage_bytes"],
                                ephemeral_storage_request_bytes=row["ephemeral_storage_request_bytes"],
                                ephemeral_storage_usage_bytes=row["ephemeral_storage_usage_bytes"],
                                gpu_usage_millicores=row["gpu_usage_millicores"],
                                restart_count=row["restart_count"],
                                owner_kind=row["owner_kind"],
                                owner_name=row["owner_name"],
                                period=row["period"],
                                timestamp=datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None,
                                duration_seconds=row["duration_seconds"],
                                grid_intensity_timestamp=datetime.fromisoformat(row["grid_intensity_timestamp"])
                                if row["grid_intensity_timestamp"]
                                else None,
                                node=row["node"],
                                node_instance_type=row["node_instance_type"],
                                node_zone=row["node_zone"],
                                emaps_zone=row["emaps_zone"],
                                is_estimated=bool(row["is_estimated"]),
                                estimation_reasons=estimation_reasons,
                                embodied_co2e_grams=row["embodied_co2e_grams"],
                                calculation_version=row["calculation_version"],
                            )
                        )
                    return metrics
        except sqlite3.Error as e:
            logging.error("Could not read combined metrics: %s", e)
            raise QueryError(f"Could not read combined metrics: {e}") from e
        except Exception as e:
            logging.error("Unexpected error reading combined metrics: %s", e)
            raise QueryError(f"Unexpected error reading combined metrics: {e}") from e

    async def aggregate_summary(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> dict:
        """
        Aggregates summary metrics directly in SQLite (no Python-side row scan).
        Significantly faster than loading all rows into CombinedMetric objects.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                if namespace:
                    query = """
                        SELECT
                            COALESCE(SUM(co2e_grams), 0)          AS total_co2e,
                            COALESCE(SUM(embodied_co2e_grams), 0) AS total_embodied,
                            COALESCE(SUM(total_cost), 0)          AS total_cost,
                            COALESCE(SUM(joules), 0)              AS total_energy,
                            COUNT(DISTINCT pod_name)               AS pod_count,
                            COUNT(DISTINCT namespace)              AS namespace_count
                        FROM combined_metrics
                        WHERE "timestamp" BETWEEN ? AND ?
                          AND namespace = ?
                    """
                    params = (start_time.isoformat(), end_time.isoformat(), namespace)
                else:
                    query = """
                        SELECT
                            COALESCE(SUM(co2e_grams), 0)          AS total_co2e,
                            COALESCE(SUM(embodied_co2e_grams), 0) AS total_embodied,
                            COALESCE(SUM(total_cost), 0)          AS total_cost,
                            COALESCE(SUM(joules), 0)              AS total_energy,
                            COUNT(DISTINCT pod_name)               AS pod_count,
                            COUNT(DISTINCT namespace)              AS namespace_count
                        FROM combined_metrics
                        WHERE "timestamp" BETWEEN ? AND ?
                    """
                    params = (start_time.isoformat(), end_time.isoformat())

                async with conn.execute(query, params) as cursor:
                    row = await cursor.fetchone()
                    return {
                        "total_co2e_grams": row["total_co2e"] or 0.0,
                        "total_embodied_co2e_grams": row["total_embodied"] or 0.0,
                        "total_cost": row["total_cost"] or 0.0,
                        "total_energy_joules": row["total_energy"] or 0.0,
                        "pod_count": row["pod_count"] or 0,
                        "namespace_count": row["namespace_count"] or 0,
                    }
        except sqlite3.Error as e:
            logging.error("aggregate_summary failed: %s", e)
            raise QueryError(f"aggregate_summary failed: {e}") from e

    async def aggregate_timeseries(
        self,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "hour",
        namespace: Optional[str] = None,
    ) -> List[dict]:
        """
        Groups time-series data by granularity directly in SQLite.
        Significantly faster than loading all rows into CombinedMetric objects.
        """
        # SQLite strftime format strings for each granularity
        _SQLITE_FORMATS = {
            "hour": "%Y-%m-%dT%H:00:00Z",
            "day": "%Y-%m-%dT00:00:00Z",
            "week": "%Y-W%W",
            "month": "%Y-%m-01T00:00:00Z",
        }
        ts_format = _SQLITE_FORMATS.get(granularity, "%Y-%m-%dT%H:00:00Z")

        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                if namespace:
                    query = f"""
                        SELECT
                            strftime('{ts_format}', "timestamp") AS ts_bucket,
                            COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                            COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                            COALESCE(SUM(total_cost), 0)          AS total_cost,
                            COALESCE(SUM(joules), 0)              AS energy_joules,
                            COALESCE(SUM(cpu_usage_millicores), 0) AS cpu_usage_millicores,
                            COALESCE(SUM(memory_usage_bytes), 0)  AS memory_usage_bytes
                        FROM combined_metrics
                        WHERE "timestamp" BETWEEN ? AND ?
                          AND namespace = ?
                        GROUP BY ts_bucket
                        ORDER BY ts_bucket
                    """
                    params = (start_time.isoformat(), end_time.isoformat(), namespace)
                else:
                    query = f"""
                        SELECT
                            strftime('{ts_format}', "timestamp") AS ts_bucket,
                            COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                            COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                            COALESCE(SUM(total_cost), 0)          AS total_cost,
                            COALESCE(SUM(joules), 0)              AS energy_joules,
                            COALESCE(SUM(cpu_usage_millicores), 0) AS cpu_usage_millicores,
                            COALESCE(SUM(memory_usage_bytes), 0)  AS memory_usage_bytes
                        FROM combined_metrics
                        WHERE "timestamp" BETWEEN ? AND ?
                        GROUP BY ts_bucket
                        ORDER BY ts_bucket
                    """
                    params = (start_time.isoformat(), end_time.isoformat())

                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [
                        {
                            "timestamp": row["ts_bucket"],
                            "co2e_grams": row["co2e_grams"],
                            "embodied_co2e_grams": row["embodied_co2e_grams"],
                            "total_cost": row["total_cost"],
                            "energy_joules": row["energy_joules"],
                            "cpu_usage_millicores": row["cpu_usage_millicores"],
                            "memory_usage_bytes": row["memory_usage_bytes"],
                        }
                        for row in rows
                    ]
        except sqlite3.Error as e:
            logging.error("aggregate_timeseries failed: %s", e)
            raise QueryError(f"aggregate_timeseries failed: {e}") from e
