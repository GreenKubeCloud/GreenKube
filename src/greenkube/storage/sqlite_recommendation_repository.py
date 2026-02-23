# src/greenkube/storage/sqlite_recommendation_repository.py
"""
SQLite implementation of the RecommendationRepository.
Persists recommendation history for trend tracking.
"""

import logging
from datetime import datetime
from typing import List, Optional

import aiosqlite

from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.storage.base_repository import RecommendationRepository
from greenkube.utils.date_utils import to_iso_z

logger = logging.getLogger(__name__)


class SQLiteRecommendationRepository(RecommendationRepository):
    """SQLite implementation for recommendation history storage."""

    def __init__(self, db_manager):
        """Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def save_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Saves recommendation records to SQLite.

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
                    priority, potential_savings_cost, potential_savings_co2e_grams,
                    current_cpu_request_millicores, recommended_cpu_request_millicores,
                    current_memory_request_bytes, recommended_memory_request_bytes,
                    cron_schedule, target_node, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            for rec in records:
                created_at_str = to_iso_z(rec.created_at) if rec.created_at else None
                await conn.execute(
                    query,
                    (
                        rec.pod_name,
                        rec.namespace,
                        rec.type.value if isinstance(rec.type, RecommendationType) else rec.type,
                        rec.description,
                        rec.reason,
                        rec.priority,
                        rec.potential_savings_cost,
                        rec.potential_savings_co2e_grams,
                        rec.current_cpu_request_millicores,
                        rec.recommended_cpu_request_millicores,
                        rec.current_memory_request_bytes,
                        rec.recommended_memory_request_bytes,
                        rec.cron_schedule,
                        rec.target_node,
                        created_at_str,
                    ),
                )
            await conn.commit()
            logger.info("Saved %d recommendation records to SQLite.", len(records))
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
            conn.row_factory = aiosqlite.Row
            query = "SELECT * FROM recommendation_history WHERE created_at >= ? AND created_at <= ?"
            params: list = [to_iso_z(start), to_iso_z(end)]

            if rec_type:
                query += " AND type = ?"
                params.append(rec_type)

            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)

            query += " ORDER BY created_at DESC"

            async with conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()

            results = []
            for row in rows:
                results.append(
                    RecommendationRecord(
                        id=row["id"],
                        pod_name=row["pod_name"],
                        namespace=row["namespace"],
                        type=RecommendationType(row["type"]),
                        description=row["description"],
                        reason=row["reason"] or "",
                        priority=row["priority"] or "medium",
                        potential_savings_cost=row["potential_savings_cost"],
                        potential_savings_co2e_grams=row["potential_savings_co2e_grams"],
                        current_cpu_request_millicores=row["current_cpu_request_millicores"],
                        recommended_cpu_request_millicores=row["recommended_cpu_request_millicores"],
                        current_memory_request_bytes=row["current_memory_request_bytes"],
                        recommended_memory_request_bytes=row["recommended_memory_request_bytes"],
                        cron_schedule=row["cron_schedule"],
                        target_node=row["target_node"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                )
            return results
