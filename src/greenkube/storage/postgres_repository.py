import json
import logging
from datetime import datetime
from typing import List, Optional

from ..core.exceptions import QueryError
from ..models.metrics import CombinedMetric
from ..storage.base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)


class PostgresCarbonIntensityRepository(CarbonIntensityRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> Optional[float]:
        try:
            async with self.db_manager.connection_scope() as conn:
                # Use $n placeholders for asyncpg
                query = """
                    SELECT carbon_intensity
                    FROM carbon_intensity
                    WHERE zone = $1 AND timestamp <= $2
                    ORDER BY timestamp DESC
                    LIMIT 1
                """
                row = await conn.fetchrow(query, zone, timestamp)
                if row:
                    return row["carbon_intensity"]
                return None
        except Exception as e:
            logger.error(f"Error getting carbon intensity from Postgres: {e}")
            raise QueryError(f"Error getting carbon intensity: {e}") from e

    async def save_history(self, history_data: list, zone: str) -> int:
        if not history_data:
            return 0

        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    INSERT INTO carbon_intensity (
                        zone, carbon_intensity, timestamp, updated_at, created_at,
                        emission_factor_type, is_estimated, estimation_method
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8
                    )
                    ON CONFLICT (zone, timestamp) DO UPDATE SET
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

                    records.append(
                        (
                            zone,
                            record.get("carbonIntensity"),
                            ts,
                            record.get("updatedAt"),
                            record.get("createdAt"),
                            record.get("emissionFactorType"),
                            record.get("isEstimated"),
                            record.get("estimationMethod"),
                        )
                    )

                await conn.executemany(query, records)
                logger.info(f"Saved {len(history_data)} records to Postgres for zone {zone}.")
                return len(history_data)
        except Exception as e:
            logger.error(f"Error saving history to Postgres: {e}")
            raise QueryError(f"Error saving history: {e}") from e

    async def write_combined_metrics(self, metrics: List[CombinedMetric]):
        if not metrics:
            return

        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    INSERT INTO CA_metrics (
                        pod_name, namespace, total_cost, co2e_grams,
                        pue, grid_intensity, joules, cpu_request,
                        memory_request, period, timestamp,
                        duration_seconds, grid_intensity_timestamp,
                        node_instance_type, node_zone,
                        emaps_zone, estimation_reasons
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11,
                        $12, $13,
                        $14, $15,
                        $16, $17::jsonb
                    )
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
                            metric.period,
                            metric.timestamp,
                            metric.duration_seconds,
                            metric.grid_intensity_timestamp,
                            metric.node_instance_type,
                            metric.node_zone,
                            metric.emaps_zone,
                            reasons_json,
                        )
                    )

                await conn.executemany(query, metrics_data)
                # No commit needed as asyncpg usually autocommits or we rely on explicit transaction
                logger.info(f"Saved {len(metrics)} combined metrics to Postgres.")
        except Exception as e:
            logger.error(f"Error writing combined metrics to Postgres: {e}")
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
                """
                results = await conn.fetch(query, start_time, end_time)

                metrics = []
                for row in results:
                    metric_data = dict(row)

                    # Deserialize estimation_reasons from JSON string
                    if "estimation_reasons" in metric_data and isinstance(metric_data["estimation_reasons"], str):
                        try:
                            metric_data["estimation_reasons"] = json.loads(metric_data["estimation_reasons"])
                        except json.JSONDecodeError:
                            metric_data["estimation_reasons"] = []

                    metrics.append(CombinedMetric(**metric_data))

                return metrics
        except Exception as e:
            logger.error(f"Error reading combined metrics from Postgres: {e}")
            raise QueryError(f"Error reading combined metrics: {e}") from e
