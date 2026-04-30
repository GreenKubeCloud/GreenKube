# src/greenkube/storage/postgres/recommendation_repository.py
"""
PostgreSQL implementation of the RecommendationRepository.
Persists recommendation history with full lifecycle management.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional

from ...models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationRecord,
    RecommendationSavingsSummary,
    RecommendationStatus,
    RecommendationType,
)
from ..base_repository import RecommendationRepository

logger = logging.getLogger(__name__)


def _row_to_record(row) -> RecommendationRecord:
    """Converts a database row to a RecommendationRecord."""
    return RecommendationRecord(
        id=row["id"],
        pod_name=row["pod_name"],
        namespace=row["namespace"],
        type=RecommendationType(row["type"]),
        description=row["description"],
        reason=row.get("reason", ""),
        priority=row.get("priority", "medium"),
        scope=row.get("scope", "pod"),
        status=RecommendationStatus(row.get("status", "active")),
        potential_savings_cost=row.get("potential_savings_cost"),
        potential_savings_co2e_grams=row.get("potential_savings_co2e_grams"),
        current_cpu_request_millicores=row.get("current_cpu_request_millicores"),
        recommended_cpu_request_millicores=row.get("recommended_cpu_request_millicores"),
        current_memory_request_bytes=row.get("current_memory_request_bytes"),
        recommended_memory_request_bytes=row.get("recommended_memory_request_bytes"),
        cron_schedule=row.get("cron_schedule"),
        target_node=row.get("target_node"),
        applied_at=row.get("applied_at"),
        actual_cpu_request_millicores=row.get("actual_cpu_request_millicores"),
        actual_memory_request_bytes=row.get("actual_memory_request_bytes"),
        carbon_saved_co2e_grams=row.get("carbon_saved_co2e_grams"),
        cost_saved=row.get("cost_saved"),
        ignored_at=row.get("ignored_at"),
        ignored_reason=row.get("ignored_reason"),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


class PostgresRecommendationRepository(RecommendationRepository):
    """PostgreSQL implementation for recommendation lifecycle storage."""

    def __init__(self, db_manager):
        """Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def save_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Saves recommendation records to PostgreSQL (append-only).

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
                    priority, scope, status,
                    potential_savings_cost, potential_savings_co2e_grams,
                    current_cpu_request_millicores, recommended_cpu_request_millicores,
                    current_memory_request_bytes, recommended_memory_request_bytes,
                    cron_schedule, target_node, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14, $15, $16, $17
                )
            """
            data = [
                (
                    r.pod_name,
                    r.namespace,
                    r.type.value if isinstance(r.type, RecommendationType) else r.type,
                    r.description,
                    r.reason,
                    r.priority,
                    r.scope,
                    r.status.value if isinstance(r.status, RecommendationStatus) else r.status,
                    r.potential_savings_cost,
                    r.potential_savings_co2e_grams,
                    r.current_cpu_request_millicores,
                    r.recommended_cpu_request_millicores,
                    r.current_memory_request_bytes,
                    r.recommended_memory_request_bytes,
                    r.cron_schedule,
                    r.target_node,
                    r.created_at,
                )
                for r in records
            ]
            await conn.executemany(query, data)
            logger.info("Saved %d recommendation records to PostgreSQL.", len(records))
            return len(records)

    async def upsert_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Inserts or updates active recommendations using (pod_name, namespace, type) as the key.

        Uses IS NOT DISTINCT FROM for NULL-safe matching so that cluster-level
        recommendations (pod_name=NULL, namespace=NULL) are correctly deduplicated.
        Ignored and applied recommendations are left untouched.

        Args:
            records: List of RecommendationRecord objects to upsert.

        Returns:
            The number of records inserted or updated.
        """
        if not records:
            return 0

        now = datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            update_query = """
                UPDATE recommendation_history SET
                    description = $1,
                    reason = $2,
                    priority = $3,
                    potential_savings_cost = $4,
                    potential_savings_co2e_grams = $5,
                    current_cpu_request_millicores = $6,
                    recommended_cpu_request_millicores = $7,
                    current_memory_request_bytes = $8,
                    recommended_memory_request_bytes = $9,
                    cron_schedule = $10,
                    target_node = $11,
                    updated_at = $12
                WHERE pod_name IS NOT DISTINCT FROM $13
                  AND namespace IS NOT DISTINCT FROM $14
                  AND type = $15
                  AND status = 'active'
            """
            insert_query = """
                INSERT INTO recommendation_history (
                    pod_name, namespace, type, description, reason,
                    priority, scope, status,
                    potential_savings_cost, potential_savings_co2e_grams,
                    current_cpu_request_millicores, recommended_cpu_request_millicores,
                    current_memory_request_bytes, recommended_memory_request_bytes,
                    cron_schedule, target_node, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14, $15, $16, $17, $18
                )
            """
            count = 0
            for r in records:
                type_val = r.type.value if isinstance(r.type, RecommendationType) else r.type
                status_val = r.status.value if isinstance(r.status, RecommendationStatus) else r.status

                result = await conn.execute(
                    update_query,
                    r.description,
                    r.reason,
                    r.priority,
                    r.potential_savings_cost,
                    r.potential_savings_co2e_grams,
                    r.current_cpu_request_millicores,
                    r.recommended_cpu_request_millicores,
                    r.current_memory_request_bytes,
                    r.recommended_memory_request_bytes,
                    r.cron_schedule,
                    r.target_node,
                    now,
                    r.pod_name,
                    r.namespace,
                    type_val,
                )
                if result == "UPDATE 0":
                    await conn.execute(
                        insert_query,
                        r.pod_name,
                        r.namespace,
                        type_val,
                        r.description,
                        r.reason,
                        r.priority,
                        r.scope,
                        status_val,
                        r.potential_savings_cost,
                        r.potential_savings_co2e_grams,
                        r.current_cpu_request_millicores,
                        r.recommended_cpu_request_millicores,
                        r.current_memory_request_bytes,
                        r.recommended_memory_request_bytes,
                        r.cron_schedule,
                        r.target_node,
                        r.created_at,
                        now,
                    )
                count += 1

            logger.info("Upserted %d recommendation records in PostgreSQL.", count)
            return count

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
            idx = 3

            if rec_type:
                query += f" AND type = ${idx}"
                params.append(rec_type)
                idx += 1

            if namespace:
                query += f" AND namespace = ${idx}"
                params.append(namespace)

            query += " ORDER BY created_at DESC"
            rows = await conn.fetch(query, *params)
            return [_row_to_record(r) for r in rows]

    async def get_active_recommendations(
        self,
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """Returns all currently active recommendations.

        Args:
            namespace: Optional namespace filter.

        Returns:
            A list of active RecommendationRecord objects.
        """
        async with self.db_manager.connection_scope() as conn:
            params: list = []
            idx = 1
            conditions = ["status = 'active'"]

            if namespace:
                conditions.append(f"namespace = ${idx}")
                params.append(namespace)

            where = " AND ".join(conditions)
            query = f"SELECT * FROM recommendation_history WHERE {where} ORDER BY priority DESC, created_at DESC"
            rows = await conn.fetch(query, *params)
            return [_row_to_record(r) for r in rows]

    async def get_ignored_recommendations(
        self,
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """Returns all permanently ignored recommendations.

        Args:
            namespace: Optional namespace filter.

        Returns:
            A list of ignored RecommendationRecord objects.
        """
        async with self.db_manager.connection_scope() as conn:
            params: list = []
            query = "SELECT * FROM recommendation_history WHERE status = 'ignored'"

            if namespace:
                query += " AND namespace = $1"
                params.append(namespace)

            query += " ORDER BY ignored_at DESC"
            rows = await conn.fetch(query, *params)
            return [_row_to_record(r) for r in rows]

    async def get_applied_recommendations(
        self,
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """Returns all applied recommendations, ordered by most recently applied.

        Args:
            namespace: Optional namespace filter.

        Returns:
            A list of applied RecommendationRecord objects.
        """
        async with self.db_manager.connection_scope() as conn:
            params: list = []
            query = "SELECT * FROM recommendation_history WHERE status = 'applied'"

            if namespace:
                query += " AND namespace = $1"
                params.append(namespace)

            query += " ORDER BY applied_at DESC"
            rows = await conn.fetch(query, *params)
            return [_row_to_record(r) for r in rows]

    async def get_recommendation_by_id(self, rec_id: int) -> Optional[RecommendationRecord]:
        """Returns a single recommendation by its database ID.

        Args:
            rec_id: The database primary key.

        Returns:
            The RecommendationRecord, or None if not found.
        """
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM recommendation_history WHERE id = $1", rec_id)
            return _row_to_record(row) if row else None

    async def apply_recommendation(self, rec_id: int, request: ApplyRecommendationRequest) -> RecommendationRecord:
        """Marks a recommendation as applied and records the actual applied values.

        If savings are not provided, the potential savings from the original recommendation
        are used as the best available estimate.

        Args:
            rec_id: The database primary key.
            request: The apply request with actual values.

        Returns:
            The updated RecommendationRecord.
        """
        now = datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM recommendation_history WHERE id = $1", rec_id)
            if not row:
                raise ValueError(f"Recommendation {rec_id} not found.")

            carbon_saved = request.carbon_saved_co2e_grams
            if carbon_saved is None:
                carbon_saved = row.get("potential_savings_co2e_grams")

            cost_saved = request.cost_saved
            if cost_saved is None:
                cost_saved = row.get("potential_savings_cost")

            updated = await conn.fetchrow(
                """
                UPDATE recommendation_history SET
                    status = 'applied',
                    applied_at = $2,
                    actual_cpu_request_millicores = $3,
                    actual_memory_request_bytes = $4,
                    carbon_saved_co2e_grams = $5,
                    cost_saved = $6,
                    updated_at = $2
                WHERE id = $1
                RETURNING *
                """,
                rec_id,
                now,
                request.actual_cpu_request_millicores,
                request.actual_memory_request_bytes,
                carbon_saved,
                cost_saved,
            )
            logger.info("Recommendation %d marked as applied.", rec_id)
            return _row_to_record(updated)

    async def ignore_recommendation(self, rec_id: int, request: IgnoreRecommendationRequest) -> RecommendationRecord:
        """Permanently ignores a recommendation.

        Args:
            rec_id: The database primary key.
            request: The ignore request with an optional reason.

        Returns:
            The updated RecommendationRecord.
        """
        now = datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE recommendation_history SET
                    status = 'ignored',
                    ignored_at = $2,
                    ignored_reason = $3,
                    updated_at = $2
                WHERE id = $1
                RETURNING *
                """,
                rec_id,
                now,
                request.reason,
            )
            if not updated:
                raise ValueError(f"Recommendation {rec_id} not found.")
            logger.info("Recommendation %d ignored. Reason: %s", rec_id, request.reason)
            return _row_to_record(updated)

    async def unignore_recommendation(self, rec_id: int) -> RecommendationRecord:
        """Reverts an ignored recommendation back to active status.

        Args:
            rec_id: The database primary key.

        Returns:
            The updated RecommendationRecord.
        """
        now = datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE recommendation_history SET
                    status = 'active',
                    ignored_at = NULL,
                    ignored_reason = NULL,
                    updated_at = $2
                WHERE id = $1
                RETURNING *
                """,
                rec_id,
                now,
            )
            if not updated:
                raise ValueError(f"Recommendation {rec_id} not found.")
            logger.info("Recommendation %d un-ignored, restored to active.", rec_id)
            return _row_to_record(updated)

    async def get_savings_summary(
        self,
        namespace: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> RecommendationSavingsSummary:
        """Returns aggregate savings from all applied recommendations.

        Args:
            namespace: Optional namespace filter.
            start: Optional inclusive lower bound on applied_at.
            end: Optional exclusive upper bound on applied_at.

        Returns:
            A RecommendationSavingsSummary with totals and per-namespace breakdown.
        """
        async with self.db_manager.connection_scope() as conn:
            params: list = []
            conditions = ["status = 'applied'"]
            if namespace:
                params.append(namespace)
                conditions.append(f"namespace = ${len(params)}")
            if start:
                params.append(start)
                conditions.append(f"applied_at >= ${len(params)}")
            if end:
                params.append(end)
                conditions.append(f"applied_at < ${len(params)}")

            where = "WHERE " + " AND ".join(conditions)

            rows = await conn.fetch(
                f"SELECT namespace, carbon_saved_co2e_grams, cost_saved FROM recommendation_history {where}",
                *params,
            )

            total_carbon = 0.0
            total_cost = 0.0
            by_ns: dict = defaultdict(lambda: {"carbon_saved_co2e_grams": 0.0, "cost_saved": 0.0, "count": 0})

            for row in rows:
                c = row.get("carbon_saved_co2e_grams") or 0.0
                s = row.get("cost_saved") or 0.0
                total_carbon += c
                total_cost += s
                ns_key = row["namespace"] or "_cluster"
                by_ns[ns_key]["carbon_saved_co2e_grams"] += c
                by_ns[ns_key]["cost_saved"] += s
                by_ns[ns_key]["count"] += 1

            return RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=total_carbon,
                total_cost_saved=total_cost,
                applied_count=len(rows),
                namespace_breakdown=[{"namespace": ns, **vals} for ns, vals in by_ns.items()],
            )
