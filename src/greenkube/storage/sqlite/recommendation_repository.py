# src/greenkube/storage/sqlite/recommendation_repository.py
"""
SQLite implementation of the RecommendationRepository.
Persists recommendation history with full lifecycle management.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite

from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationRecord,
    RecommendationSavingsSummary,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.base_repository import RecommendationRepository
from greenkube.utils.date_utils import to_iso_z

logger = logging.getLogger(__name__)


def _row_to_record(row) -> RecommendationRecord:
    """Converts an aiosqlite Row to a RecommendationRecord."""

    def _dt(val) -> Optional[datetime]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        return datetime.fromisoformat(val.replace("Z", "+00:00"))

    return RecommendationRecord(
        id=row["id"],
        pod_name=row["pod_name"],
        namespace=row["namespace"],
        type=RecommendationType(row["type"]),
        description=row["description"],
        reason=row["reason"] or "",
        priority=row["priority"] or "medium",
        scope=row["scope"] or "pod",
        status=RecommendationStatus(row["status"] if row["status"] else "active"),
        potential_savings_cost=row["potential_savings_cost"],
        potential_savings_co2e_grams=row["potential_savings_co2e_grams"],
        current_cpu_request_millicores=row["current_cpu_request_millicores"],
        recommended_cpu_request_millicores=row["recommended_cpu_request_millicores"],
        current_memory_request_bytes=row["current_memory_request_bytes"],
        recommended_memory_request_bytes=row["recommended_memory_request_bytes"],
        cron_schedule=row["cron_schedule"],
        target_node=row["target_node"],
        applied_at=_dt(row["applied_at"]),
        actual_cpu_request_millicores=row["actual_cpu_request_millicores"],
        actual_memory_request_bytes=row["actual_memory_request_bytes"],
        carbon_saved_co2e_grams=row["carbon_saved_co2e_grams"],
        cost_saved=row["cost_saved"],
        ignored_at=_dt(row["ignored_at"]),
        ignored_reason=row["ignored_reason"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def _type_value(record: RecommendationRecord) -> str:
    """Returns the persisted recommendation type value."""
    return record.type.value if isinstance(record.type, RecommendationType) else record.type


def _status_value(record: RecommendationRecord) -> str:
    """Returns the persisted recommendation status value."""
    return record.status.value if isinstance(record.status, RecommendationStatus) else record.status


def _identity_key(record: RecommendationRecord) -> tuple:
    """Returns the stable identity used to refresh recommendation lifecycle rows."""
    return (
        record.scope or "pod",
        record.namespace,
        record.pod_name,
        record.target_node,
        _type_value(record),
    )


def _identity_where_clause(record: RecommendationRecord) -> tuple[str, list]:
    """Builds a SQLite WHERE clause for NULL-safe recommendation identity matching."""
    return (
        "COALESCE(scope, 'pod') = ? "
        "AND ((namespace = ?) OR (namespace IS NULL AND ? IS NULL)) "
        "AND ((pod_name = ?) OR (pod_name IS NULL AND ? IS NULL)) "
        "AND ((target_node = ?) OR (target_node IS NULL AND ? IS NULL)) "
        "AND type = ? AND status = 'active'",
        [
            record.scope or "pod",
            record.namespace,
            record.namespace,
            record.pod_name,
            record.pod_name,
            record.target_node,
            record.target_node,
            _type_value(record),
        ],
    )


class SQLiteRecommendationRepository(RecommendationRepository):
    """SQLite implementation for recommendation lifecycle storage."""

    def __init__(self, db_manager):
        """Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def save_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Saves recommendation records to SQLite (append-only).

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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            for r in records:
                await conn.execute(
                    query,
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
                        to_iso_z(r.created_at) if r.created_at else None,
                    ),
                )
            await conn.commit()
            logger.info("Saved %d recommendation records to SQLite.", len(records))
            return len(records)

    async def upsert_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Inserts or updates active recommendations using their full target identity.

        Ignored and applied recommendations are left untouched.

        Args:
            records: List of RecommendationRecord objects to upsert.

        Returns:
            The number of records inserted or updated.
        """
        if not records:
            return 0

        now = to_iso_z(datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            for r in records:
                status_val = _status_value(r)
                identity_where, identity_params = _identity_where_clause(r)
                row = await conn.execute(
                    f"SELECT id, status FROM recommendation_history WHERE {identity_where}",
                    identity_params,
                )
                existing = await row.fetchone()
                if existing:
                    await conn.execute(
                        """
                        UPDATE recommendation_history SET
                            description = ?, reason = ?, priority = ?,
                            scope = ?,
                            potential_savings_cost = ?, potential_savings_co2e_grams = ?,
                            current_cpu_request_millicores = ?, recommended_cpu_request_millicores = ?,
                            current_memory_request_bytes = ?, recommended_memory_request_bytes = ?,
                            cron_schedule = ?, target_node = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            r.description,
                            r.reason,
                            r.priority,
                            r.scope,
                            r.potential_savings_cost,
                            r.potential_savings_co2e_grams,
                            r.current_cpu_request_millicores,
                            r.recommended_cpu_request_millicores,
                            r.current_memory_request_bytes,
                            r.recommended_memory_request_bytes,
                            r.cron_schedule,
                            r.target_node,
                            now,
                            existing["id"],
                        ),
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO recommendation_history (
                            pod_name, namespace, type, description, reason,
                            priority, scope, status,
                            potential_savings_cost, potential_savings_co2e_grams,
                            current_cpu_request_millicores, recommended_cpu_request_millicores,
                            current_memory_request_bytes, recommended_memory_request_bytes,
                            cron_schedule, target_node, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            r.pod_name,
                            r.namespace,
                            r.type.value if isinstance(r.type, RecommendationType) else r.type,
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
                            to_iso_z(r.created_at) if r.created_at else now,
                            now,
                        ),
                    )
            await conn.commit()
            logger.info("Upserted %d recommendation records in SQLite.", len(records))
            return len(records)

    async def reconcile_active_recommendations(
        self,
        records: List[RecommendationRecord],
        namespace: Optional[str] = None,
    ) -> int:
        """Marks active recommendations absent from the latest generated set as stale."""
        current_keys = {_identity_key(record) for record in records}
        now = to_iso_z(datetime.now(timezone.utc))

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            params: list = []
            query = (
                "SELECT id, pod_name, namespace, type, scope, target_node "
                "FROM recommendation_history WHERE status = 'active'"
            )
            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

            stale_ids = []
            for row in rows:
                row_record = RecommendationRecord(
                    id=row["id"],
                    pod_name=row["pod_name"],
                    namespace=row["namespace"],
                    type=RecommendationType(row["type"]),
                    scope=row["scope"] or "pod",
                    target_node=row["target_node"],
                    description="placeholder",
                )
                if _identity_key(row_record) not in current_keys:
                    stale_ids.append(row["id"])

            if not stale_ids:
                return 0

            placeholders = ", ".join("?" for _ in stale_ids)
            await conn.execute(
                f"UPDATE recommendation_history SET status = 'stale', updated_at = ? WHERE id IN ({placeholders})",
                [now, *stale_ids],
            )
            await conn.commit()
            logger.info("Marked %d SQLite recommendation record(s) as stale.", len(stale_ids))
            return len(stale_ids)

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
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
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
            conn.row_factory = aiosqlite.Row
            conditions = ["status = 'active'"]
            params: list = []

            if namespace:
                conditions.append("namespace = ?")
                params.append(namespace)

            where = " AND ".join(conditions)
            query = f"SELECT * FROM recommendation_history WHERE {where} ORDER BY priority DESC, created_at DESC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
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
            conn.row_factory = aiosqlite.Row
            params: list = []
            query = "SELECT * FROM recommendation_history WHERE status = 'ignored'"

            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)

            query += " ORDER BY ignored_at DESC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
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
            conn.row_factory = aiosqlite.Row
            params: list = []
            query = "SELECT * FROM recommendation_history WHERE status = 'applied'"

            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)

            query += " ORDER BY applied_at DESC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [_row_to_record(r) for r in rows]

    async def get_recommendation_by_id(self, rec_id: int) -> Optional[RecommendationRecord]:
        """Returns a single recommendation by its database ID.

        Args:
            rec_id: The database primary key.

        Returns:
            The RecommendationRecord, or None if not found.
        """
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))
            row = await cursor.fetchone()
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
        now = to_iso_z(datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))
            row = await cursor.fetchone()
            if not row:
                raise ValueError(f"Recommendation {rec_id} not found.")

            carbon_saved = request.carbon_saved_co2e_grams
            if carbon_saved is None:
                carbon_saved = row["potential_savings_co2e_grams"]

            cost_saved = request.cost_saved
            if cost_saved is None:
                cost_saved = row["potential_savings_cost"]

            await conn.execute(
                """
                UPDATE recommendation_history SET
                    status = 'applied',
                    applied_at = ?,
                    actual_cpu_request_millicores = ?,
                    actual_memory_request_bytes = ?,
                    carbon_saved_co2e_grams = ?,
                    cost_saved = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    now,
                    request.actual_cpu_request_millicores,
                    request.actual_memory_request_bytes,
                    carbon_saved,
                    cost_saved,
                    now,
                    rec_id,
                ),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))
            updated = await cursor.fetchone()
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
        now = to_iso_z(datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT id FROM recommendation_history WHERE id = ?", (rec_id,))
            if not await cursor.fetchone():
                raise ValueError(f"Recommendation {rec_id} not found.")

            await conn.execute(
                """
                UPDATE recommendation_history SET
                    status = 'ignored',
                    ignored_at = ?,
                    ignored_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, request.reason, now, rec_id),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))
            updated = await cursor.fetchone()
            logger.info("Recommendation %d ignored. Reason: %s", rec_id, request.reason)
            return _row_to_record(updated)

    async def unignore_recommendation(self, rec_id: int) -> RecommendationRecord:
        """Reverts an ignored recommendation back to active status.

        Args:
            rec_id: The database primary key.

        Returns:
            The updated RecommendationRecord.
        """
        now = to_iso_z(datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT id FROM recommendation_history WHERE id = ?", (rec_id,))
            if not await cursor.fetchone():
                raise ValueError(f"Recommendation {rec_id} not found.")

            await conn.execute(
                """
                UPDATE recommendation_history SET
                    status = 'active',
                    ignored_at = NULL,
                    ignored_reason = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, rec_id),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))
            updated = await cursor.fetchone()
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
            conn.row_factory = aiosqlite.Row
            query = (
                "SELECT namespace, carbon_saved_co2e_grams, cost_saved "
                "FROM recommendation_history WHERE status = 'applied'"
            )
            params: list = []

            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)
            if start:
                query += " AND applied_at >= ?"
                params.append(to_iso_z(start))
            if end:
                query += " AND applied_at < ?"
                params.append(to_iso_z(end))

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

            total_carbon = 0.0
            total_cost = 0.0
            by_ns: dict = defaultdict(lambda: {"carbon_saved_co2e_grams": 0.0, "cost_saved": 0.0, "count": 0})

            for row in rows:
                c = row["carbon_saved_co2e_grams"] or 0.0
                s = row["cost_saved"] or 0.0
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
