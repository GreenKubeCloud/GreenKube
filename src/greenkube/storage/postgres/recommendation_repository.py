# src/greenkube/storage/postgres/recommendation_repository.py
"""
PostgreSQL implementation of the RecommendationRepository.
Persists recommendation history with full lifecycle management.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ...core.recommendation_ranking import normalize_savings_metric
from ...core.recommendation_realization import estimate_realized_savings, refresh_applied_recommendation
from ...models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationEvent,
    RecommendationRecord,
    RecommendationSavingsSummary,
    RecommendationStatus,
    RecommendationType,
)
from ..base_repository import RecommendationRepository
from ..recommendation_mapper import (
    ACTIVE_UPSERT_COLUMNS,
    RECORD_COLUMNS,
    record_values,
    row_to_record,
)
from ..sqlite.recommendation_repository import MUTABLE_COLUMNS

logger = logging.getLogger(__name__)


def _encode_value(value):
    """Encodes a Python value for an asyncpg bind parameter."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
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
        record.scope or "pod",
        record.namespace,
        record.pod_name,
        record.target_node,
        _type_value(record),
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

        columns = RECORD_COLUMNS
        query = "INSERT INTO recommendation_history ({}) VALUES ({})".format(
            ", ".join(columns),
            ", ".join(f"${index}" for index in range(1, len(columns) + 1)),
        )
        async with self.db_manager.connection_scope() as conn:
            data = []
            for r in records:
                values = record_values(r)
                data.append(tuple(values[column] for column in columns))
            await conn.executemany(query, data)
            logger.info("Saved %d recommendation records to PostgreSQL.", len(records))
            return len(records)

    async def upsert_recommendations(self, records: List[RecommendationRecord]) -> int:
        """Inserts or updates active recommendations using their full target identity.

        Uses IS NOT DISTINCT FROM for NULL-safe matching so namespace, node,
        pod, and workload recommendations are all deduplicated on the right target.
        Ignored recommendations are left untouched. Matching applied recommendations
        are refreshed so future savings attribution reflects the latest observed state.

        Args:
            records: List of RecommendationRecord objects to upsert.

        Returns:
            The number of records inserted or updated.
        """
        if not records:
            return 0

        now = datetime.now(timezone.utc)
        set_columns = ACTIVE_UPSERT_COLUMNS + ("updated_at",)
        where_offset = len(set_columns)
        update_query = (
            "UPDATE recommendation_history SET {} "
            "WHERE COALESCE(scope, 'pod') = ${} "
            "AND namespace IS NOT DISTINCT FROM ${} "
            "AND pod_name IS NOT DISTINCT FROM ${} "
            "AND target_node IS NOT DISTINCT FROM ${} "
            "AND type = ${} "
            "AND status = 'active'"
        ).format(
            ", ".join(f"{column} = ${index}" for index, column in enumerate(set_columns, start=1)),
            where_offset + 1,
            where_offset + 2,
            where_offset + 3,
            where_offset + 4,
            where_offset + 5,
        )
        insert_query = "INSERT INTO recommendation_history ({}) VALUES ({})".format(
            ", ".join(RECORD_COLUMNS),
            ", ".join(f"${index}" for index in range(1, len(RECORD_COLUMNS) + 1)),
        )
        async with self.db_manager.connection_scope() as conn:
            applied_select_query = """
                SELECT * FROM recommendation_history
                WHERE COALESCE(scope, 'pod') = $1
                  AND namespace IS NOT DISTINCT FROM $2
                  AND pod_name IS NOT DISTINCT FROM $3
                  AND target_node IS NOT DISTINCT FROM $4
                  AND type = $5
                  AND status = 'applied'
                ORDER BY applied_at DESC
                LIMIT 1
            """
            applied_update_query = """
                UPDATE recommendation_history SET
                    actual_cpu_request_millicores = $1,
                    actual_memory_request_bytes = $2,
                    carbon_saved_co2e_grams = $3,
                    cost_saved = $4,
                    updated_at = $5
                WHERE id = $6
            """
            count = 0
            for r in records:
                type_val = _type_value(r)
                status_val = _status_value(r)

                values = record_values(r)
                update_params = [values[column] for column in ACTIVE_UPSERT_COLUMNS] + [
                    now,
                    r.scope or "pod",
                    r.namespace,
                    r.pod_name,
                    r.target_node,
                    type_val,
                ]
                result = await conn.execute(update_query, *update_params)
                if result != "UPDATE 0":
                    count += 1
                    continue

                applied_row = await conn.fetchrow(
                    applied_select_query,
                    r.scope or "pod",
                    r.namespace,
                    r.pod_name,
                    r.target_node,
                    type_val,
                )
                if applied_row:
                    refreshed = refresh_applied_recommendation(row_to_record(applied_row), r, observed_at=now)
                    await conn.execute(
                        applied_update_query,
                        refreshed.actual_cpu_request_millicores,
                        refreshed.actual_memory_request_bytes,
                        refreshed.carbon_saved_co2e_grams,
                        refreshed.cost_saved,
                        refreshed.updated_at,
                        refreshed.id,
                    )
                    count += 1
                    continue

                values["status"] = status_val
                values["created_at"] = values["created_at"] or now
                values["updated_at"] = now
                await conn.execute(insert_query, *(values[column] for column in RECORD_COLUMNS))
                count += 1

            logger.info("Upserted %d recommendation records in PostgreSQL.", count)
            return count

    async def reconcile_active_recommendations(
        self,
        records: List[RecommendationRecord],
        namespace: Optional[str] = None,
    ) -> int:
        """Marks active recommendations absent from the latest generated set as stale."""
        current_keys = {_identity_key(record) for record in records}
        now = datetime.now(timezone.utc)

        async with self.db_manager.connection_scope() as conn:
            params: list = []
            query = (
                "SELECT id, pod_name, namespace, type, scope, target_node "
                "FROM recommendation_history WHERE status = 'active'"
            )
            if namespace:
                params.append(namespace)
                query += f" AND namespace = ${len(params)}"

            rows = await conn.fetch(query, *params)
            stale_ids = []
            for row in rows:
                data = dict(row)
                row_record = RecommendationRecord(
                    id=data["id"],
                    pod_name=data["pod_name"],
                    namespace=data["namespace"],
                    type=RecommendationType(data["type"]),
                    scope=data.get("scope") or "pod",
                    target_node=data.get("target_node"),
                    description="placeholder",
                )
                if _identity_key(row_record) not in current_keys:
                    stale_ids.append(data["id"])

            if not stale_ids:
                return 0

            await conn.execute(
                "UPDATE recommendation_history SET status = 'stale', updated_at = $1 WHERE id = ANY($2::int[])",
                now,
                stale_ids,
            )
            logger.info("Marked %d PostgreSQL recommendation record(s) as stale.", len(stale_ids))
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
            params: list = []
            idx = 1
            conditions = ["status = 'active'"]

            if namespace:
                conditions.append(f"namespace = ${idx}")
                params.append(namespace)

            where = " AND ".join(conditions)
            query = f"SELECT * FROM recommendation_history WHERE {where} ORDER BY priority DESC, created_at DESC"
            rows = await conn.fetch(query, *params)
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
            params: list = []
            conditions = ["status = 'active'", f"COALESCE({primary_column}, 0) > 0"]

            if namespace:
                params.append(namespace)
                conditions.append(f"namespace = ${len(params)}")

            params.append(bounded_limit)
            limit_placeholder = f"${len(params)}"
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
                LIMIT {limit_placeholder}
            """
            rows = await conn.fetch(query, *params)
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
            params: list = []
            query = "SELECT * FROM recommendation_history WHERE status = 'ignored'"

            if namespace:
                query += " AND namespace = $1"
                params.append(namespace)

            query += " ORDER BY ignored_at DESC"
            rows = await conn.fetch(query, *params)
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
            params: list = []
            query = (
                "SELECT * FROM recommendation_history "
                "WHERE status IN ('applied', 'verifying', 'verified', 'rollback_review')"
            )

            if namespace:
                query += " AND namespace = $1"
                params.append(namespace)

            query += " ORDER BY applied_at DESC"
            rows = await conn.fetch(query, *params)
            return [row_to_record(r) for r in rows]

    async def get_recommendations_by_statuses(
        self,
        statuses: List[str],
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """Returns recommendations in any of the given lifecycle statuses."""
        if not statuses:
            return []
        params: list = [list(statuses)]
        query = "SELECT * FROM recommendation_history WHERE status = ANY($1::text[])"
        if namespace:
            params.append(namespace)
            query += f" AND namespace = ${len(params)}"
        query += " ORDER BY created_at DESC"

        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(query, *params)
            return [row_to_record(r) for r in rows]

    async def get_applied_recommendations_stats(self) -> List[dict]:
        """Return aggregated applied-recommendation stats via SQL GROUP BY.

        Returns one dict per (type, namespace) pair with keys:
        ``type``, ``namespace``, ``count``, ``total_co2e_grams``, ``total_cost_dollars``.
        """
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch("""
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
            return [dict(r) for r in rows]

    async def get_recommendation_by_id(self, rec_id: int) -> Optional[RecommendationRecord]:
        """Returns a single recommendation by its database ID.

        Args:
            rec_id: The database primary key.

        Returns:
            The RecommendationRecord, or None if not found.
        """
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM recommendation_history WHERE id = $1", rec_id)
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
        now = datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM recommendation_history WHERE id = $1", rec_id)
            if not row:
                raise ValueError(f"Recommendation {rec_id} not found.")

            record = row_to_record(row)
            carbon_saved, cost_saved = estimate_realized_savings(record, request)
            method = application_method or request.application_method or "manual"

            updated = await conn.fetchrow(
                """
                UPDATE recommendation_history SET
                    status = 'applied',
                    applied_at = $2,
                    actual_cpu_request_millicores = $3,
                    actual_memory_request_bytes = $4,
                    carbon_saved_co2e_grams = $5,
                    cost_saved = $6,
                    application_method = $7,
                    baseline = $8,
                    verification_status = 'pending',
                    verification_window_start = NULL,
                    verification_window_end = NULL,
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
                method,
                json.dumps(baseline) if baseline is not None else None,
            )
            logger.info("Recommendation %d marked as applied (%s).", rec_id, method)
            return row_to_record(updated)

    async def update_recommendation_fields(self, rec_id: int, updates: dict) -> RecommendationRecord:
        """Updates a subset of mutable recommendation columns."""
        filtered = {k: v for k, v in updates.items() if k in MUTABLE_COLUMNS}
        if not filtered:
            raise ValueError("No mutable recommendation columns supplied.")

        set_clause = ", ".join(f"{column} = ${index}" for index, column in enumerate(filtered, start=1))
        params = [_encode_value(value) for value in filtered.values()]
        params.append(rec_id)

        async with self.db_manager.connection_scope() as conn:
            updated = await conn.fetchrow(
                f"UPDATE recommendation_history SET {set_clause} WHERE id = ${len(params)} RETURNING *",
                *params,
            )
            if not updated:
                raise ValueError(f"Recommendation {rec_id} not found.")
            return row_to_record(updated)

    async def record_event(self, event: RecommendationEvent) -> RecommendationEvent:
        """Appends a recommendation lifecycle event to the audit trail."""
        created_at = event.created_at or datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO recommendation_events
                    (recommendation_id, event_type, actor, payload, created_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, created_at
                """,
                event.recommendation_id,
                event.event_type,
                event.actor,
                json.dumps(event.payload or {}),
                created_at,
            )
            return event.model_copy(update={"id": row["id"], "created_at": row["created_at"]})

    async def get_events(self, rec_id: int) -> List[RecommendationEvent]:
        """Returns the audit trail for a recommendation, oldest first."""
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recommendation_events WHERE recommendation_id = $1 ORDER BY created_at ASC, id ASC",
                rec_id,
            )
            events: List[RecommendationEvent] = []
            for row in rows:
                data = dict(row)
                payload = data.get("payload")
                try:
                    payload = json.loads(payload) if payload else {}
                except (TypeError, ValueError):
                    payload = {}
                events.append(
                    RecommendationEvent(
                        id=data["id"],
                        recommendation_id=data["recommendation_id"],
                        event_type=data["event_type"],
                        actor=data["actor"],
                        payload=payload,
                        created_at=data["created_at"],
                    )
                )
            return events

    async def expire_recommendations(self, now: Optional[datetime] = None) -> List[RecommendationRecord]:
        """Marks active recommendations past their TTL as expired."""
        reference = now or datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                """
                UPDATE recommendation_history
                SET status = 'expired', updated_at = $1
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at < $1
                RETURNING *
                """,
                reference,
            )
            if rows:
                logger.info("Expired %d PostgreSQL recommendation record(s).", len(rows))
            return [row_to_record(row) for row in rows]

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
            return row_to_record(updated)

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
            params: list = []
            conditions = ["status IN ('applied', 'verifying', 'verified', 'rollback_review')"]
            if namespace:
                params.append(namespace)
                conditions.append(f"namespace = ${len(params)}")
            if end:
                params.append(end)
                conditions.append(f"applied_at < ${len(params)}")

            where = "WHERE " + " AND ".join(conditions)

            rows = await conn.fetch(
                "SELECT namespace, status, carbon_saved_co2e_grams, cost_saved, "
                f"measured_co2e_saved_grams, measured_cost_saved FROM recommendation_history {where}",
                *params,
            )

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
                data = dict(row)
                is_verified = data["status"] == "verified" and data.get("measured_co2e_saved_grams") is not None
                if is_verified:
                    c = data.get("measured_co2e_saved_grams") or 0.0
                    s = data.get("measured_cost_saved") or 0.0
                    measured_carbon += c
                    measured_cost += s
                else:
                    c = data.get("carbon_saved_co2e_grams") or 0.0
                    s = data.get("cost_saved") or 0.0
                    prorated_carbon += c
                    prorated_cost += s
                ns_key = data["namespace"] or "_cluster"
                by_ns[ns_key]["carbon_saved_co2e_grams"] += c
                by_ns[ns_key]["cost_saved"] += s
                by_ns[ns_key]["measured_carbon_saved_co2e_grams"] += (
                    data.get("measured_co2e_saved_grams") or 0.0 if is_verified else 0.0
                )
                by_ns[ns_key]["measured_cost_saved"] += data.get("measured_cost_saved") or 0.0 if is_verified else 0.0
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
                verified_count=sum(1 for row in rows if dict(row)["status"] == "verified"),
            )
