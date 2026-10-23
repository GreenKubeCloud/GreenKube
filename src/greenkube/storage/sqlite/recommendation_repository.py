# src/greenkube/storage/sqlite/recommendation_repository.py
"""
SQLite implementation of the RecommendationRepository.
Persists recommendation history with full lifecycle management.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

import aiosqlite

from greenkube.core.recommendation_ranking import normalize_savings_metric
from greenkube.core.recommendation_realization import estimate_realized_savings, refresh_applied_recommendation
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationEvent,
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

#: Columns that lifecycle services may update through ``update_recommendation_fields``.
MUTABLE_COLUMNS: frozenset = frozenset(
    {
        "pod_name",
        "container_name",
        "namespace",
        "type",
        "description",
        "reason",
        "priority",
        "scope",
        "status",
        "potential_savings_cost",
        "potential_savings_co2e_grams",
        "current_cpu_request_millicores",
        "recommended_cpu_request_millicores",
        "current_memory_request_bytes",
        "recommended_memory_request_bytes",
        "cron_schedule",
        "target_node",
        "identity_version",
        "fingerprint",
        "source",
        "source_ref",
        "sources",
        "superseded_by",
        "capability",
        "owner_kind",
        "owner_name",
        "evidence",
        "risk_level",
        "risk_factors",
        "confidence",
        "effort",
        "ranking_score",
        "ranking_factors",
        "patch",
        "expires_at",
        "reversible",
        "requires_restart",
        "applied_at",
        "actual_cpu_request_millicores",
        "actual_memory_request_bytes",
        "carbon_saved_co2e_grams",
        "cost_saved",
        "ignored_at",
        "ignored_reason",
        "application_method",
        "verified_at",
        "verification_status",
        "verification_window_start",
        "verification_window_end",
        "baseline",
        "measured_co2e_saved_grams",
        "measured_cost_saved",
        "savings_realized",
        "updated_at",
    }
)


def _encode_value(value):
    """Encodes a Python value for a SQLite bind parameter."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return to_iso_z(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            default=lambda item: (
                item.model_dump(mode="json")
                if hasattr(item, "model_dump")
                else item.isoformat()
                if isinstance(item, datetime)
                else str(item)
            ),
        )
    return value


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
        record.fingerprint,
        record.scope or "pod",
        record.namespace,
        record.pod_name,
        record.container_name,
        record.target_node,
        _type_value(record),
    )


def _identity_where_clause(record: RecommendationRecord, status: str = "active") -> tuple[str, list]:
    """Builds a SQLite WHERE clause for NULL-safe recommendation identity matching."""
    return (
        "fingerprint = ? AND "
        "COALESCE(scope, 'pod') = ? "
        "AND ((namespace = ?) OR (namespace IS NULL AND ? IS NULL)) "
        "AND ((pod_name = ?) OR (pod_name IS NULL AND ? IS NULL)) "
        "AND ((container_name = ?) OR (container_name IS NULL AND ? IS NULL)) "
        "AND ((target_node = ?) OR (target_node IS NULL AND ? IS NULL)) "
        "AND type = ? AND status = ?",
        [
            record.fingerprint,
            record.scope or "pod",
            record.namespace,
            record.namespace,
            record.pod_name,
            record.pod_name,
            record.container_name,
            record.container_name,
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
                "SELECT id, pod_name, container_name, namespace, type, scope, target_node, fingerprint "
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
                    container_name=row["container_name"],
                    namespace=row["namespace"],
                    type=RecommendationType(row["type"]),
                    scope=row["scope"] or "pod",
                    target_node=row["target_node"],
                    fingerprint=row["fingerprint"],
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
        """Returns applied recommendations and their verification states.

        Includes ``applied``, ``verifying``, ``verified`` and
        ``rollback_review`` records so the Realized Savings view can display
        projected and measured outcomes side by side.

        Args:
            namespace: Optional namespace filter.

        Returns:
            A list of RecommendationRecord objects ordered by most recently applied.
        """
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            params: list = []
            query = (
                "SELECT * FROM recommendation_history "
                "WHERE status IN ('applied', 'verifying', 'verified', 'rollback_review')"
            )

            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)

            query += " ORDER BY applied_at DESC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [row_to_record(r) for r in rows]

    async def get_recommendations_by_statuses(
        self,
        statuses: List[str],
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """Returns recommendations in any of the given lifecycle statuses."""
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        query = f"SELECT * FROM recommendation_history WHERE status IN ({placeholders})"
        params: list = list(statuses)
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        query += " ORDER BY created_at DESC"

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
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
                WHERE status IN ('applied', 'verifying', 'verified', 'rollback_review')
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

    async def apply_recommendation(
        self,
        rec_id: int,
        request: ApplyRecommendationRequest,
        *,
        baseline: Optional[dict] = None,
        application_method: Optional[str] = None,
    ) -> RecommendationRecord:
        """Marks a recommendation as applied and records the actual applied values.

        If savings are not provided, the potential savings from the original recommendation
        are used as the best available estimate.

        Args:
            rec_id: The database primary key.
            request: The apply request with actual values.
            baseline: Frozen pre-apply metrics captured for verification.
            application_method: How the change landed (manual, detected, pr_merge, ...).

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
            method = application_method or request.application_method or "manual"

            await conn.execute(
                """
                UPDATE recommendation_history SET
                    status = 'applied',
                    applied_at = ?,
                    actual_cpu_request_millicores = ?,
                    actual_memory_request_bytes = ?,
                    carbon_saved_co2e_grams = ?,
                    cost_saved = ?,
                    application_method = ?,
                    baseline = ?,
                    verification_status = 'pending',
                    verification_window_start = NULL,
                    verification_window_end = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    now,
                    request.actual_cpu_request_millicores,
                    request.actual_memory_request_bytes,
                    carbon_saved,
                    cost_saved,
                    method,
                    json.dumps(baseline.model_dump(mode="json") if hasattr(baseline, "model_dump") else baseline)
                    if baseline is not None
                    else None,
                    now,
                    rec_id,
                ),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))
            updated = await cursor.fetchone()
            logger.info("Recommendation %d marked as applied (%s).", rec_id, method)
            return row_to_record(updated)

    async def update_recommendation_fields(self, rec_id: int, updates: dict) -> RecommendationRecord:
        """Updates a subset of mutable recommendation columns."""
        filtered = {k: v for k, v in updates.items() if k in MUTABLE_COLUMNS}
        if not filtered:
            raise ValueError("No mutable recommendation columns supplied.")

        set_clause = ", ".join(f"{column} = ?" for column in filtered)
        params = [_encode_value(value) for value in filtered.values()]
        params.append(rec_id)

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT id FROM recommendation_history WHERE id = ?", (rec_id,))
            if not await cursor.fetchone():
                raise ValueError(f"Recommendation {rec_id} not found.")
            await conn.execute(
                f"UPDATE recommendation_history SET {set_clause} WHERE id = ?",
                params,
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))
            updated = await cursor.fetchone()
            return row_to_record(updated)

    async def record_event(self, event: RecommendationEvent) -> RecommendationEvent:
        """Appends a recommendation lifecycle event to the audit trail."""
        created_at = to_iso_z(event.created_at or datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO recommendation_events
                    (recommendation_id, event_type, actor, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.recommendation_id,
                    event.event_type,
                    event.actor,
                    json.dumps(
                        event.payload.model_dump(mode="json")
                        if hasattr(event.payload, "model_dump")
                        else event.payload or {}
                    ),
                    created_at,
                ),
            )
            await conn.commit()
            return event.model_copy(
                update={"id": cursor.lastrowid, "created_at": event.created_at or datetime.now(timezone.utc)}
            )

    async def transition_and_record_event(self, rec_id: int, updates: dict, event_type: str, actor: str, payload: dict):
        """Atomically update a recommendation and append its lifecycle event."""
        filtered = {k: v for k, v in updates.items() if k in MUTABLE_COLUMNS}
        if not filtered:
            raise ValueError("No mutable recommendation columns supplied.")
        now = datetime.now(timezone.utc)
        filtered.setdefault("updated_at", now)
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            params = [_encode_value(value) for value in filtered.values()] + [rec_id]
            await conn.execute(
                f"UPDATE recommendation_history SET {', '.join(f'{k} = ?' for k in filtered)} WHERE id = ?",
                params,
            )
            await conn.execute(
                """INSERT INTO recommendation_events
                (recommendation_id, event_type, actor, payload, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (rec_id, event_type, actor, json.dumps(payload), to_iso_z(now)),
            )
            await conn.commit()
            row = await (await conn.execute("SELECT * FROM recommendation_history WHERE id = ?", (rec_id,))).fetchone()
            return row_to_record(row)

    async def get_events(self, rec_id: int) -> List[RecommendationEvent]:
        """Returns the audit trail for a recommendation, oldest first."""
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_events WHERE recommendation_id = ? ORDER BY created_at ASC, id ASC",
                (rec_id,),
            )
            rows = await cursor.fetchall()
            events: List[RecommendationEvent] = []
            for row in rows:
                payload = row["payload"]
                try:
                    payload = json.loads(payload) if payload else {}
                except (TypeError, ValueError):
                    payload = {}
                events.append(
                    RecommendationEvent(
                        id=row["id"],
                        recommendation_id=row["recommendation_id"],
                        event_type=row["event_type"],
                        actor=row["actor"],
                        payload=payload,
                        created_at=row["created_at"],
                    )
                )
            return events

    async def expire_recommendations(self, now: Optional[datetime] = None) -> List[RecommendationRecord]:
        """Marks active recommendations past their TTL as expired."""
        reference = to_iso_z(now or datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT * FROM recommendation_history
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at < ?
                """,
                (reference,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ", ".join("?" for _ in ids)
            await conn.execute(
                f"UPDATE recommendation_history SET status = 'expired', updated_at = ? WHERE id IN ({placeholders})",
                [reference, *ids],
            )
            await conn.commit()
            logger.info("Expired %d SQLite recommendation record(s).", len(ids))
            return [row_to_record(row).model_copy(update={"status": RecommendationStatus.EXPIRED}) for row in rows]

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
                "SELECT namespace, status, carbon_saved_co2e_grams, cost_saved, "
                "measured_co2e_saved_grams, measured_cost_saved "
                "FROM recommendation_history "
                "WHERE status IN ('applied', 'verifying', 'verified', 'rollback_review')"
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

            measured_carbon = 0.0
            measured_cost = 0.0
            prorated_carbon = 0.0
            prorated_cost = 0.0
            by_ns: dict = defaultdict(
                lambda: {
                    "carbon_saved_co2e_grams": 0.0,
                    "cost_saved": 0.0,
                    "measured_carbon_saved_co2e_grams": 0.0,
                    "measured_cost_saved": 0.0,
                    "count": 0,
                }
            )

            for row in rows:
                is_verified = row["status"] == "verified" and row["measured_co2e_saved_grams"] is not None
                if is_verified:
                    c = row["measured_co2e_saved_grams"] or 0.0
                    s = row["measured_cost_saved"] or 0.0
                    measured_carbon += c
                    measured_cost += s
                else:
                    c = row["carbon_saved_co2e_grams"] or 0.0
                    s = row["cost_saved"] or 0.0
                    prorated_carbon += c
                    prorated_cost += s
                ns_key = row["namespace"] or "_cluster"
                by_ns[ns_key]["carbon_saved_co2e_grams"] += c
                by_ns[ns_key]["cost_saved"] += s
                by_ns[ns_key]["measured_carbon_saved_co2e_grams"] += (
                    row["measured_co2e_saved_grams"] or 0.0 if is_verified else 0.0
                )
                by_ns[ns_key]["measured_cost_saved"] += row["measured_cost_saved"] or 0.0 if is_verified else 0.0
                by_ns[ns_key]["count"] += 1

            return RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=measured_carbon + prorated_carbon,
                total_cost_saved=measured_cost + prorated_cost,
                applied_count=len(rows),
                namespace_breakdown=[{"namespace": ns, **vals} for ns, vals in by_ns.items()],
                measured_carbon_saved_co2e_grams=measured_carbon,
                measured_cost_saved=measured_cost,
                prorated_carbon_saved_co2e_grams=prorated_carbon,
                prorated_cost_saved=prorated_cost,
                verified_count=sum(1 for row in rows if row["status"] == "verified"),
            )
