import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import List

import aiosqlite

from greenkube.models.metrics import CombinedMetric
from greenkube.utils.date_utils import ensure_utc, to_iso_z

from ..core.exceptions import QueryError
from .base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)


class SQLiteCarbonIntensityRepository(CarbonIntensityRepository):
    """
    Implementation of the repository for SQLite.
    Handles all database interactions for carbon intensity data.
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
        Only considers data from the last 48 hours to avoid using stale values.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row

                # Normalize the query timestamp to ensure it matches the stored format (Z suffix)
                try:
                    dt = ensure_utc(timestamp)
                    normalized_ts = to_iso_z(dt)
                except ValueError:
                    # If parsing fails, fallback to using the timestamp as-is (though it likely won't match)
                    dt = None
                    normalized_ts = timestamp

                # Define lookback window (48 hours)
                if dt:
                    lookback_limit = to_iso_z(dt - timedelta(hours=48))
                    query = """
                        SELECT carbon_intensity
                        FROM carbon_intensity_history
                        WHERE zone = ? AND datetime <= ? AND datetime >= ?
                        ORDER BY datetime DESC
                        LIMIT 1
                    """
                    params = (zone, normalized_ts, lookback_limit)
                else:
                    # Fallback: use original query without time bound if timestamp parsing fails
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
            logger.error(f"Database error in get_for_zone_at_time for zone {zone} at {timestamp}: {e}")
            raise QueryError(f"Database error in get_for_zone_at_time: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error in get_for_zone_at_time: {e}")
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
                        logging.warning(f"Skipping invalid record (not a dict): {record}")
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
                        logging.error(f"Could not save record for zone {zone} at {record.get('datetime')}: {e}")
                    except Exception as e:
                        # Catch potential errors from record.get() if record structure is unexpected
                        logging.error(f"Unexpected error processing record {record}: {e}")

                await conn.commit()
                return saved_count
        except sqlite3.Error as e:
            logging.error(f"Failed to commit transaction: {e}")
            raise QueryError(f"Failed to commit transaction: {e}") from e
        except Exception as e:
            logging.error(f"Unexpected error in save_history: {e}")
            raise QueryError(f"Unexpected error in save_history: {e}") from e

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
                                  cpu_request, memory_request, period, "timestamp", duration_seconds,
                                  grid_intensity_timestamp, node_instance_type, node_zone, emaps_zone,
                                  is_estimated, estimation_reasons)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(pod_name, namespace, "timestamp") DO NOTHING;
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
                                metric.period,
                                timestamp_iso,
                                metric.duration_seconds,
                                grid_intensity_timestamp_iso,
                                metric.node_instance_type,
                                metric.node_zone,
                                metric.emaps_zone,
                                metric.is_estimated,
                                json.dumps(metric.estimation_reasons) if metric.estimation_reasons else "[]",
                            ),
                        )
                        saved_count += cursor.rowcount
                    except sqlite3.Error as e:
                        logging.error(f"Could not save combined metric for pod {metric.pod_name}: {e}")
                    except Exception as e:
                        logging.error(f"Unexpected error processing combined metric {metric.pod_name}: {e}")

                await conn.commit()
                return saved_count
        except sqlite3.Error as e:
            logging.error(f"Failed to commit transaction for combined_metrics: {e}")
            raise QueryError(f"Failed to commit combined_metrics: {e}") from e
        except Exception as e:
            logging.error(f"Unexpected error in write_combined_metrics: {e}")
            raise QueryError(f"Unexpected error in write_combined_metrics: {e}") from e

    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    """
                    SELECT pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                           cpu_request, memory_request, period, "timestamp", duration_seconds, grid_intensity_timestamp,
                           node_instance_type, node_zone, emaps_zone, is_estimated, estimation_reasons
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
                                period=row["period"],
                                timestamp=datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None,
                                duration_seconds=row["duration_seconds"],
                                grid_intensity_timestamp=datetime.fromisoformat(row["grid_intensity_timestamp"])
                                if row["grid_intensity_timestamp"]
                                else None,
                                node_instance_type=row["node_instance_type"],
                                node_zone=row["node_zone"],
                                emaps_zone=row["emaps_zone"],
                                is_estimated=bool(row["is_estimated"]),
                                estimation_reasons=estimation_reasons,
                            )
                        )
                    return metrics
        except sqlite3.Error as e:
            logging.error(f"Could not read combined metrics: {e}")
            raise QueryError(f"Could not read combined metrics: {e}") from e
        except Exception as e:
            logging.error(f"Unexpected error reading combined metrics: {e}")
            raise QueryError(f"Unexpected error reading combined metrics: {e}") from e
