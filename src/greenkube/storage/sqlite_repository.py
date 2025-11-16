import logging
import sqlite3
from datetime import datetime

from greenkube.models.metrics import CombinedMetric

from .base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)


class SQLiteCarbonIntensityRepository(CarbonIntensityRepository):
    """
    Implementation of the repository for SQLite.
    Handles all database interactions for carbon intensity data.
    """

    def __init__(self, connection):
        """
        Initializes the repository with a database connection.

        Args:
            connection: An active sqlite3 connection object.
        """
        self.conn = connection
        if not self.conn:
            logging.error("SQLite connection is not available upon initialization.")

    def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.
        """
        if not self.conn:
            logging.error("SQLite connection is not available for get_for_zone_at_time.")
            return None
        try:
            cursor = self.conn.cursor()
            query = """
                SELECT carbon_intensity
                FROM carbon_intensity_history
                WHERE zone = ? AND datetime <= ?
                ORDER BY datetime DESC
                LIMIT 1
            """
            cursor.execute(query, (zone, timestamp))
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            logging.error(f"Database error in get_for_zone_at_time for zone {zone} at {timestamp}: {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error in get_for_zone_at_time: {e}")
            return None

    def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves historical carbon intensity data to the SQLite database.
        It ignores records that would be duplicates based on zone and datetime.
        """
        if not self.conn:
            # Use logging for errors
            logging.error("SQLite connection is not available for save_history.")
            return 0

        cursor = self.conn.cursor()
        saved_count = 0

        for record in history_data:
            # Basic validation that record is a dictionary
            if not isinstance(record, dict):
                logging.warning(f"Skipping invalid record (not a dict): {record}")
                continue

            try:
                # Use default value None if key is missing
                cursor.execute(
                    """
                    INSERT INTO carbon_intensity_history
                        (zone, carbon_intensity, datetime, updated_at, created_at,
                         emission_factor_type, is_estimated, estimation_method)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(zone, datetime) DO NOTHING;
                """,
                    (
                        zone,
                        record.get("carbonIntensity"),
                        record.get("datetime"),
                        record.get("updatedAt"),
                        record.get("createdAt"),
                        record.get("emissionFactorType"),
                        record.get("isEstimated"),
                        record.get("estimationMethod"),
                    ),
                )
                # cursor.rowcount will be 1 for a successful insert, 0 for conflict/no insert
                saved_count += cursor.rowcount
            except sqlite3.Error as e:
                # Use logging for errors
                logging.error(f"Could not save record for zone {zone} at {record.get('datetime')}: {e}")
            except Exception as e:
                # Catch potential errors from record.get() if record structure is unexpected
                logging.error(f"Unexpected error processing record {record}: {e}")

        try:
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to commit transaction: {e}")
            # Depending on strategy, you might want to rollback or handle differently
            return 0  # Indicate commit failure if necessary

        return saved_count

    def write_combined_metrics(self, metrics: list) -> int:
        if not self.conn:
            logging.error("SQLite connection is not available for write_combined_metrics.")
            return 0

        cursor = self.conn.cursor()
        saved_count = 0

        for metric in metrics:
            try:
                # Handle datetime objects that might be None before formatting
                timestamp_iso = metric.get("timestamp").isoformat() if metric.get("timestamp") else None
                grid_intensity_timestamp_iso = (
                    metric.get("grid_intensity_timestamp").isoformat()
                    if metric.get("grid_intensity_timestamp")
                    else None
                )

                cursor.execute(
                    """
                    INSERT INTO combined_metrics
                        (pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                         cpu_request, memory_request, period, "timestamp", duration_seconds, grid_intensity_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pod_name, namespace, "timestamp") DO NOTHING;
                """,
                    (
                        metric.get("pod_name"),
                        metric.get("namespace"),
                        metric.get("total_cost"),
                        metric.get("co2e_grams"),
                        metric.get("pue"),
                        metric.get("grid_intensity"),
                        metric.get("joules"),
                        metric.get("cpu_request"),
                        metric.get("memory_request"),
                        metric.get("period"),
                        timestamp_iso,
                        metric.get("duration_seconds"),
                        grid_intensity_timestamp_iso,
                    ),
                )
                saved_count += cursor.rowcount
            except sqlite3.Error as e:
                logging.error(f"Could not save combined metric for pod {metric.pod_name}: {e}")
            except Exception as e:
                logging.error(f"Unexpected error processing combined metric {metric.pod_name}: {e}")

        try:
            self.conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Failed to commit transaction for combined_metrics: {e}")
            return 0

        return saved_count

    def read_combined_metrics(self, start_time, end_time) -> list[CombinedMetric]:
        if not self.conn:
            logging.error("SQLite connection is not available for read_combined_metrics.")
            return []

        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                       cpu_request, memory_request, period, "timestamp", duration_seconds, grid_intensity_timestamp
                FROM combined_metrics
                WHERE "timestamp" BETWEEN ? AND ?
            """,
                (start_time.isoformat(), end_time.isoformat()),
            )
            rows = cursor.fetchall()
            metrics = [
                CombinedMetric(
                    pod_name=row[0],
                    namespace=row[1],
                    total_cost=row[2],
                    co2e_grams=row[3],
                    pue=row[4],
                    grid_intensity=row[5],
                    joules=row[6],
                    cpu_request=row[7],
                    memory_request=row[8],
                    period=row[9],
                    timestamp=datetime.fromisoformat(row[10]) if row[10] else None,
                    duration_seconds=row[11],
                    grid_intensity_timestamp=datetime.fromisoformat(row[12]) if row[12] else None,
                )
                for row in rows
            ]
            return metrics
        except sqlite3.Error as e:
            logging.error(f"Could not read combined metrics: {e}")
            return []
        except Exception as e:
            logging.error(f"Unexpected error reading combined metrics: {e}")
            return []
