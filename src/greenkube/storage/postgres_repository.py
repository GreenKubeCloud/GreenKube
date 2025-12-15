import json
import logging
from datetime import datetime
from typing import List, Optional

from psycopg2.extras import RealDictCursor

from ..core.exceptions import QueryError
from ..models.metrics import CombinedMetric
from ..storage.base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)


class PostgresCarbonIntensityRepository(CarbonIntensityRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    def get_for_zone_at_time(self, zone: str, time: datetime) -> Optional[dict]:
        """
        Retrieves the carbon intensity for a given zone and time.
        """
        try:
            with self.db_manager.connection_scope() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    # Find the closest record before or at the given time
                    query = """
                        SELECT * FROM carbon_intensity_history
                        WHERE zone = %s AND datetime <= %s
                        ORDER BY datetime DESC
                        LIMIT 1
                    """
                    cursor.execute(query, (zone, time))
                    result = cursor.fetchone()
                    return dict(result) if result else None
        except Exception as e:
            logger.error(f"Error fetching carbon intensity from Postgres: {e}")
            raise QueryError(f"Error fetching carbon intensity: {e}") from e

    def save_history(self, history_data: List[dict]):
        """
        Saves a list of carbon intensity records to the database.
        """
        if not history_data:
            return

        try:
            with self.db_manager.connection_scope() as conn:
                with conn.cursor() as cursor:
                    query = """
                        INSERT INTO carbon_intensity_history
                            (zone, carbon_intensity, datetime, updated_at, created_at,
                             emission_factor_type, is_estimated, estimation_method)
                        VALUES (%(zone)s, %(carbon_intensity)s, %(datetime)s, %(updated_at)s, %(created_at)s,
                                %(emission_factor_type)s, %(is_estimated)s, %(estimation_method)s)
                        ON CONFLICT(zone, datetime)
                        DO UPDATE SET
                            carbon_intensity = EXCLUDED.carbon_intensity,
                            updated_at = EXCLUDED.updated_at,
                            is_estimated = EXCLUDED.is_estimated,
                            estimation_method = EXCLUDED.estimation_method,
                            emission_factor_type = EXCLUDED.emission_factor_type;
                    """
                    cursor.executemany(query, history_data)
                    conn.commit()
                    logger.info(f"Saved {len(history_data)} records to Postgres.")
        except Exception as e:
            logger.error(f"Error saving history to Postgres: {e}")
            raise QueryError(f"Error saving history: {e}") from e

    def write_combined_metrics(self, metrics: List[CombinedMetric]):
        """
        Writes combined metrics to the database.
        """
        if not metrics:
            return

        try:
            with self.db_manager.connection_scope() as conn:
                with conn.cursor() as cursor:
                    query = """
                        INSERT INTO combined_metrics (
                            pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity,
                            joules, cpu_request, memory_request, period, timestamp, duration_seconds,
                            grid_intensity_timestamp, node_instance_type, node_zone, emaps_zone,
                            is_estimated, estimation_reasons
                        ) VALUES (
                            %(pod_name)s, %(namespace)s, %(total_cost)s, %(co2e_grams)s, %(pue)s, %(grid_intensity)s,
                            %(joules)s, %(cpu_request)s, %(memory_request)s, %(period)s, %(timestamp)s,
                            %(duration_seconds)s, %(grid_intensity_timestamp)s, %(node_instance_type)s,
                            %(node_zone)s, %(emaps_zone)s, %(is_estimated)s, %(estimation_reasons)s
                        )
                        ON CONFLICT (pod_name, namespace, timestamp) DO NOTHING
                    """

                    # Convert Pydantic models to dicts for insertion
                    metrics_data = []
                    for metric in metrics:
                        data = metric.model_dump()
                        # Serialize estimation_reasons to JSON string
                        if "estimation_reasons" in data and isinstance(data["estimation_reasons"], list):
                            data["estimation_reasons"] = json.dumps(data["estimation_reasons"])
                        metrics_data.append(data)

                    cursor.executemany(query, metrics_data)
                    conn.commit()
                    logger.info(f"Saved {len(metrics)} combined metrics to Postgres.")
        except Exception as e:
            logger.error(f"Error saving combined metrics to Postgres: {e}")
            raise QueryError(f"Error saving combined metrics: {e}") from e

    def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        """
        Reads combined metrics from the database within a time range.
        """
        try:
            with self.db_manager.connection_scope() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    query = """
                        SELECT * FROM combined_metrics
                        WHERE timestamp >= %s AND timestamp <= %s
                    """
                    cursor.execute(query, (start_time, end_time))
                    results = cursor.fetchall()

                    metrics = []

                    for row in results:
                        # Deserialize estimation_reasons from JSON string
                        if "estimation_reasons" in row and isinstance(row["estimation_reasons"], str):
                            try:
                                row["estimation_reasons"] = json.loads(row["estimation_reasons"])
                            except json.JSONDecodeError:
                                row["estimation_reasons"] = []

                        # Ensure timestamp fields are correctly handled if needed,
                        # though psycopg2 usually handles datetime objects well.
                        metrics.append(CombinedMetric(**row))

                    return metrics
        except Exception as e:
            logger.error(f"Error reading combined metrics from Postgres: {e}")
            raise QueryError(f"Error reading combined metrics: {e}") from e
