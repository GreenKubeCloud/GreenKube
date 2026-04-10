# src/greenkube/storage/postgres_recommendation_repository.py
"""
PostgreSQL implementation of the RecommendationRepository.
Persists recommendation history for trend tracking.
"""

import logging
from datetime import datetime
from typing import List, Optional

from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.storage.base_repository import RecommendationRepository

logger = logging.getLogger(__name__)


class PostgresRecommendationRepository(RecommendationRepository):
    """PostgreSQL implementation for recommendation history storage."""

    def __init__(self, db_manager):
        """Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def save_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Saves recommendation records to PostgreSQL.

        Args:
            records: A list of RecommendationRecord objects to persist.

        Returns:
            The number of records saved.
        """
        if not records:
            return 0

        async with self.db_manager.connection_scope() as conn:
            query = """
                INSERT INTO recommendation_history (
                    pod_name, namespace, type, description, reason,
                    priority, scope, potential_savings_cost, potential_savings_co2e_grams,
                    current_cpu_request_millicores, recommended_cpu_request_millicores,
                    current_memory_request_bytes, recommended_memory_request_bytes,
                    cron_schedule, target_node, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16
                )
            """
            data = []
            for rec in records:
                data.append(
                    (
                        rec.pod_name,
                        rec.namespace,
                        rec.type.value if isinstance(rec.type, RecommendationType) else rec.type,
                        rec.description,
                        rec.reason,
                        rec.priority,
                        rec.scope,
                        rec.potential_savings_cost,
                        rec.potential_savings_co2e_grams,
                        rec.current_cpu_request_millicores,
                        rec.recommended_cpu_request_millicores,
                        rec.current_memory_request_bytes,
                        rec.recommended_memory_request_bytes,
                        rec.cron_schedule,
                        rec.target_node,
                        rec.created_at,
                    )
                )
            await conn.executemany(query, data)
            logger.info("Saved %d recommendation records to PostgreSQL.", len(records))
            return len(records)

    async def get_recommendations(
        self,
        start: datetime,
        end: datetime,
        rec_type: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """Retrieves recommendation records within a time range.

        Args:
            start: Start datetime (inclusive).
            end: End datetime (inclusive).
            rec_type: Optional filter by recommendation type.
            namespace: Optional filter by namespace.

        Returns:
            A list of RecommendationRecord objects.
        """
        async with self.db_manager.connection_scope() as conn:
            query = "SELECT * FROM recommendation_history WHERE created_at >= $1 AND created_at <= $2"
            params: list = [start, end]
            param_idx = 3

            if rec_type:
                query += f" AND type = ${param_idx}"
                params.append(rec_type)
                param_idx += 1

            if namespace:
                query += f" AND namespace = ${param_idx}"
                params.append(namespace)
                param_idx += 1

            query += " ORDER BY created_at DESC"

            rows = await conn.fetch(query, *params)

            results = []
            for row in rows:
                results.append(
                    RecommendationRecord(
                        id=row["id"],
                        pod_name=row["pod_name"],
                        namespace=row["namespace"],
                        type=RecommendationType(row["type"]),
                        description=row["description"],
                        reason=row.get("reason", ""),
                        priority=row.get("priority", "medium"),
                        scope=row.get("scope", "pod"),
                        potential_savings_cost=row.get("potential_savings_cost"),
                        potential_savings_co2e_grams=row.get("potential_savings_co2e_grams"),
                        current_cpu_request_millicores=row.get("current_cpu_request_millicores"),
                        recommended_cpu_request_millicores=row.get("recommended_cpu_request_millicores"),
                        current_memory_request_bytes=row.get("current_memory_request_bytes"),
                        recommended_memory_request_bytes=row.get("recommended_memory_request_bytes"),
                        cron_schedule=row.get("cron_schedule"),
                        target_node=row.get("target_node"),
                        created_at=row["created_at"],
                    )
                )
            return results
