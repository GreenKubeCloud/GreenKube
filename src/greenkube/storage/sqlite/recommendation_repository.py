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

from greenkube.core.recommendation_ranking import normalize_savings_metric
from greenkube.core.recommendation_realization import estimate_realized_savings, refresh_applied_recommendation
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationRecord,
    RecommendationSavingsSummary,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.base_repository import RecommendationRepository
from greenkube.storage.recommendation_mapper import (
    ACTIVE_UPSERT_COLUMNS,
    RECORD_COLUMNS,
    record_values,
    row_to_record,
)
from greenkube.utils.date_utils import to_iso_z

logger = logging.getLogger(__name__)


def _type_value(record: RecommendationRecord) -> str:
    """Returns the persisted recommendation type value."""
    return record.type.value if isinstance(record.type, RecommendationType) else record.type


def _status_value(record: RecommendationRecord) -> str:
    """Returns the persisted recommendation status value."""
    return record.status.value if isinstance(record.status, RecommendationStatus) else record.status


def _savings_columns(savings_metric: str) -> tuple[str, str]:
    """Returns primary and secondary savings columns for a ranked query."""
    metric = normalize_savings_metric(savings_metric)
    if metric == "cost":
        return "potential_savings_cost", "potential_savings_co2e_grams"
    return "potential_savings_co2e_grams", "potential_savings_cost"


def _bounded_limit(limit: int) -> int:
    """Keep recommendation ranking limits in a practical range."""
    return max(1, min(int(limit), 50))


def _identity_key(record: RecommendationRecord) -> tuple:
    """Returns the stable identity used to refresh recommendation lifecycle rows."""
    return (
        record.scope or "pod",
        record.namespace,
        record.pod_name,
        record.target_node,
        _type_value(record),
    )


def _identity_where_clause(record: RecommendationRecord, status: str = "active") -> tuple[str, list]:
    """Builds a SQLite WHERE clause for NULL-safe recommendation identity matching."""
    return (
        "COALESCE(scope, 'pod') = ? "
        "AND ((namespace = ?) OR (namespace IS NULL AND ? IS NULL)) "
        "AND ((pod_name = ?) OR (pod_name IS NULL AND ? IS NULL)) "
        "AND ((target_node = ?) OR (target_node IS NULL AND ? IS NULL)) "
        "AND type = ? AND status = ?",
        [
            record.scope or "pod",
            record.namespace,
            record.namespace,
            record.pod_name,
            record.pod_name,
            record.target_node,
            record.target_node,
            _type_value(record),
            status,
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

        columns = RECORD_COLUMNS
        query = "INSERT INTO recommendation_history ({}) VALUES ({})".format(
            ", ".join(columns),
            ", ".join("?" for _ in columns),
        )
        async with self.db_manager.connection_scope() as conn:
            for r in records:
                values = record_values(r, encode_datetime=to_iso_z)
                await conn.execute(query, tuple(values[column] for column in columns))
            await conn.commit()
            logger.info("Saved %d recommendation records to SQLite.", len(records))
            return len(records)

    async def upsert_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Inserts or updates active recommendations using their full target identity.

        Ignored recommendations are left untouched. Matching applied recommendations
        are refreshed so future savings attribution reflects the latest observed state.

        Args:
            records: List of RecommendationRecord objects to upsert.

        Returns:
            The number of records inserted or updated.
        """
        if not records:
            return 0

        now = to_iso_z(datetime.now(timezone.utc))
        set_columns = ACTIVE_UPSERT_COLUMNS + ("updated_at",)
        update_query = "UPDATE recommendation_history SET {} WHERE id = ?".format(
            ", ".join(f"{column} = ?" for column in set_columns)
        )
        insert_query = "INSERT INTO recommendation_history ({}) VALUES ({})".format(
            ", ".join(RECORD_COLUMNS),
            ", ".join("?" for _ in RECORD_COLUMNS),
        )
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
                    values = record_values(r, encode_datetime=to_iso_z)
                    params = [values[column] for column in ACTIVE_UPSERT_COLUMNS] + [now, existing["id"]]
                    await conn.execute(update_query, params)
                    continue

                applied_where, applied_params = _identity_where_clause(r, status="applied")
                cursor = await conn.execute(
                    f"SELECT * FROM recommendation_history WHERE {applied_where} ORDER BY applied_at DESC LIMIT 1",
                    applied_params,
                )
                applied_row = await cursor.fetchone()
                if applied_row:
                    refreshed = refresh_applied_recommendation(
                        row_to_record(applied_row), r, observed_at=datetime.now(timezone.utc)
                    )
                    await conn.execute(
                        """
                        UPDATE recommendation_history SET
                            actual_cpu_request_millicores = ?,
                            actual_memory_request_bytes = ?,
                            carbon_saved_co2e_grams = ?,
                            cost_saved = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            refreshed.actual_cpu_request_millicores,
                            refreshed.actual_memory_request_bytes,
                            refreshed.carbon_saved_co2e_grams,
                            refreshed.cost_saved,
                            to_iso_z(refreshed.updated_at) if refreshed.updated_at else now,
                            refreshed.id,
                        ),
                    )
                else:
                    values = record_values(r, encode_datetime=to_iso_z)
                    values["status"] = status_val
                    values["created_at"] = values["created_at"] or now
                    values["updated_at"] = now
                    await conn.execute(insert_query, tuple(values[column] for column in RECORD_COLUMNS))
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
            return [row_to_record(r) for r in rows]

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
            return [row_to_record(r) for r in rows]

    async def get_top_recommendations(
        self,
        limit: int = 5,
        savings_metric: str = "co2",
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """Returns active recommendations ranked by projected annual savings."""
        primary_column, secondary_column = _savings_columns(savings_metric)
        bounded_limit = _bounded_limit(limit)

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            conditions = ["status = 'active'", f"COALESCE({primary_column}, 0) > 0"]
            params: list = []

            if namespace:
                conditions.append("namespace = ?")
                params.append(namespace)

            where = " AND ".join(conditions)
            query = f"""
                SELECT * FROM recommendation_history
                WHERE {where}
                ORDER BY COALESCE({primary_column}, 0) DESC,
                         COALESCE({secondary_column}, 0) DESC,
                         CASE LOWER(COALESCE(priority, 'medium'))
                            WHEN 'critical' THEN 4
                            WHEN 'high' THEN 3
                            WHEN 'medium' THEN 2
                            WHEN 'low' THEN 1
                            ELSE 0
                         END DESC,
                         COALESCE(updated_at, created_at) DESC,
                         id DESC
                LIMIT ?
            """
            params.append(bounded_limit)
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [row_to_record(r) for r in rows]

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
            return [row_to_record(r) for r in rows]

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
            return [row_to_record(r) for r in rows]

    async def get_applied_recommendations_stats(self) -> List[dict]:
        """Return aggregated applied-recommendation stats via SQL GROUP BY.

        Returns one dict per (type, namespace) pair with keys:
        ``type``, ``namespace``, ``count``, ``total_co2e_grams``, ``total_cost_dollars``.
        """
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT
                    type,
                    COALESCE(namespace, '_cluster') AS namespace,
                    COUNT(*) AS count,
                    COALESCE(SUM(carbon_saved_co2e_grams), 0) AS total_co2e_grams,
                    COALESCE(SUM(cost_saved), 0) AS total_cost_dollars
                FROM recommendation_history
                WHERE status = 'applied'
                GROUP BY type, COALESCE(namespace, '_cluster')
            """)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

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
            return row_to_record(row) if row else None

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

            record = row_to_record(row)
            carbon_saved, cost_saved = estimate_realized_savings(record, request)

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
            return row_to_record(updated)

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
            return row_to_record(updated)

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
            return row_to_record(updated)

    async def get_savings_summary(
        self,
        namespace: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> RecommendationSavingsSummary:
        """Returns fallback aggregate savings from applied recommendations.

        Savings continue after a recommendation is applied, so this fallback
        includes records applied before ``end``. Use the savings ledger for
        exact period totals.

        Args:
            namespace: Optional namespace filter.
            start: Optional requested window start, ignored by this fallback.
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
