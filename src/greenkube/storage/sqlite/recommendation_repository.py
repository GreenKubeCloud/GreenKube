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
from typing import Collection, Dict, List, Mapping, Optional

import aiosqlite

from greenkube.core.recommendation_ranking import normalize_savings_metric
from greenkube.core.recommendation_realization import (
    RECORDED_CHANGE_STATUSES,
    absorbs_regenerated_recommendation,
    estimate_realized_savings,
    refresh_applied_recommendation,
)
from greenkube.core.recommendation_reconciliation import may_mark_stale
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationEvent,
    RecommendationRecord,
    RecommendationSavingsSummary,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.models.recommendation_v2 import RecommendationRevision
from greenkube.storage.base_repository import RecommendationRepository
from greenkube.storage.recommendation_mapper import (
    ACTIVE_UPSERT_COLUMNS,
    RECORD_COLUMNS,
    record_values,
    row_to_record,
    sources_from_json,
    to_json,
)
from greenkube.storage.recommendation_revisions import (
    QUEUED,
    SUPERSEDED,
    backfill_update,
    decide,
    is_frozen,
    revision_from_row,
    revision_values,
)
from greenkube.utils.date_utils import to_iso_z

logger = logging.getLogger(__name__)

#: Status predicate of the active listing, optionally with recommendations whose PR is open.
_ACTIVE_LISTING_PREDICATE = {False: "status = 'active'", True: "status IN ('active', 'pr_open')"}

#: Default order of active lists (E2, BUG-20): ranking score, then projected
#: cost savings, NULLs last on both backends, then a stable id tie-break.
ACTIVE_ORDER_BY = (
    "ranking_score IS NULL, ranking_score DESC, potential_savings_cost IS NULL, potential_savings_cost DESC, id ASC"
)

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


def _identity_where_clause(record: RecommendationRecord, status: str | tuple[str, ...] = "active") -> tuple[str, list]:
    """Builds a SQLite WHERE clause for NULL-safe recommendation identity matching."""
    statuses = (status,) if isinstance(status, str) else tuple(status)
    return (
        "fingerprint = ? AND "
        "COALESCE(scope, 'pod') = ? "
        "AND ((namespace = ?) OR (namespace IS NULL AND ? IS NULL)) "
        "AND ((pod_name = ?) OR (pod_name IS NULL AND ? IS NULL)) "
        "AND ((container_name = ?) OR (container_name IS NULL AND ? IS NULL)) "
        "AND ((target_node = ?) OR (target_node IS NULL AND ? IS NULL)) "
        "AND type = ? AND status IN ({})".format(", ".join("?" for _ in statuses)),
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
            *statuses,
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

        Ignored recommendations are left untouched. A recommendation whose pull
        request is open (``pr_open``) is frozen: its baseline and proposal are
        never rewritten and no duplicate is inserted. A matching applied
        recommendation only has ``updated_at`` refreshed (and, for issues that
        reappeared, its savings stopped); its ``current_*`` baseline and the
        ``actual_*`` values detected after the change are never rewritten. A
        rightsizing that restates a change already verifying or verified
        (see :func:`absorbs_regenerated_recommendation`) is dropped.

        Args:
            records: List of RecommendationRecord objects to upsert.

        Returns:
            The number of records inserted or updated.
        """
        if not records:
            return 0

        now = to_iso_z(datetime.now(timezone.utc))
        set_columns = ACTIVE_UPSERT_COLUMNS + ("revision", "updated_at")
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
                    f"SELECT * FROM recommendation_history WHERE {identity_where}",
                    identity_params,
                )
                existing = await row.fetchone()
                if existing:
                    stored = row_to_record(existing)
                    decision = decide(stored, r, frozen=is_frozen(existing["status"], existing["action_status"]))
                    if decision.action == "queue":
                        # A record never changes under an open plan (E7).
                        await self._queue_revision(conn, stored, r, decision.revision, now)
                        continue
                    await self._settle_revision(conn, stored, decision.action == "revise", now)
                    values = record_values(r, encode_datetime=to_iso_z)
                    params = [values[column] for column in ACTIVE_UPSERT_COLUMNS] + [
                        decision.revision,
                        now,
                        existing["id"],
                    ]
                    await conn.execute(update_query, params)
                    continue

                pr_open_where, pr_open_params = _identity_where_clause(r, status=RecommendationStatus.PR_OPEN.value)
                cursor = await conn.execute(
                    f"SELECT * FROM recommendation_history WHERE {pr_open_where} LIMIT 1",
                    pr_open_params,
                )
                pr_open_row = await cursor.fetchone()
                if pr_open_row:
                    # Frozen while its pull request is open (or merged and syncing): newer advice is queued.
                    stored = row_to_record(pr_open_row)
                    decision = decide(stored, r, frozen=True)
                    if decision.action == "queue":
                        await self._queue_revision(conn, stored, r, decision.revision, now)
                    else:
                        await self._settle_revision(conn, stored, False, now)
                    continue

                recorded_where, recorded_params = _identity_where_clause(r, status=RECORDED_CHANGE_STATUSES)
                cursor = await conn.execute(
                    f"SELECT * FROM recommendation_history WHERE {recorded_where} "
                    "ORDER BY applied_at IS NULL, applied_at DESC, id DESC LIMIT 1",
                    recorded_params,
                )
                recorded_row = await cursor.fetchone()
                applied_row = (
                    recorded_row
                    if recorded_row is not None and recorded_row["status"] == RecommendationStatus.APPLIED.value
                    else None
                )
                if recorded_row is not None and applied_row is None:
                    if absorbs_regenerated_recommendation(row_to_record(recorded_row), r):
                        # The scan restates a change already recorded (lagging rollup):
                        # never offer it again nor record it as a second change.
                        continue
                if applied_row:
                    refreshed = refresh_applied_recommendation(
                        row_to_record(applied_row), r, observed_at=datetime.now(timezone.utc)
                    )
                    await conn.execute(
                        """
                        UPDATE recommendation_history SET
                            carbon_saved_co2e_grams = ?,
                            cost_saved = ?,
                            updated_at = ?
                        WHERE id = ? AND status = 'applied'
                        """,
                        (
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

    @staticmethod
    async def _queue_revision(
        conn, stored: RecommendationRecord, incoming: RecommendationRecord, revision: int, now: str
    ) -> None:
        """Keep newer advice behind an open plan as the (single) queued revision."""
        values = revision_values(incoming)
        await conn.execute(
            "DELETE FROM recommendation_revisions WHERE recommendation_id = ? AND state = ? AND revision <> ?",
            (stored.id, QUEUED, revision),
        )
        await conn.execute(
            """
            INSERT INTO recommendation_revisions
                (recommendation_id, revision, state, proposal, consensus, snapshot, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (recommendation_id, revision) DO UPDATE SET
                state = excluded.state,
                proposal = excluded.proposal,
                consensus = excluded.consensus,
                snapshot = excluded.snapshot,
                updated_at = excluded.updated_at
            """,
            (
                stored.id,
                revision,
                QUEUED,
                to_json(values["proposal"]),
                to_json(values["consensus"]),
                to_json(values["snapshot"]),
                now,
                now,
            ),
        )

    @staticmethod
    async def _settle_revision(conn, stored: RecommendationRecord, revise: bool, now: str) -> None:
        """Drop queued advice; on a new revision keep the replaced proposal as ``superseded``."""
        await conn.execute(
            "DELETE FROM recommendation_revisions WHERE recommendation_id = ? AND state = ?",
            (stored.id, QUEUED),
        )
        if not revise:
            return
        values = revision_values(stored)
        await conn.execute(
            """
            INSERT INTO recommendation_revisions
                (recommendation_id, revision, state, proposal, consensus, snapshot, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (recommendation_id, revision) DO NOTHING
            """,
            (
                stored.id,
                max(1, stored.revision),
                SUPERSEDED,
                to_json(values["proposal"]),
                to_json(values["consensus"]),
                to_json(values["snapshot"]),
                to_iso_z(stored.updated_at or stored.created_at) if (stored.updated_at or stored.created_at) else now,
                now,
            ),
        )

    async def get_revisions(self, rec_id: int) -> List[RecommendationRevision]:
        """Revisions the record no longer carries (superseded and queued), newest first (E7)."""
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_revisions WHERE recommendation_id = ? ORDER BY revision DESC",
                (rec_id,),
            )
            rows = await cursor.fetchall()
        return [revision_from_row(row) for row in rows]

    async def get_queued_revisions(self, rec_ids: Collection[int]) -> Dict[int, RecommendationRevision]:
        """The queued revision of each record, when one waits behind an open plan."""
        ids = sorted({int(rec_id) for rec_id in rec_ids})
        if not ids:
            return {}
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_revisions WHERE state = ? AND recommendation_id IN ({})".format(
                    ", ".join("?" for _ in ids)
                ),
                (QUEUED, *ids),
            )
            rows = await cursor.fetchall()
        return {revision.recommendation_id: revision for revision in map(revision_from_row, rows)}

    async def pin_plan_revisions(self, plan_id: str, pins: Mapping[int, int]) -> Dict[int, int]:
        """Record the revision a plan implements per recommendation; the first pin wins (E7)."""
        if pins:
            now = to_iso_z(datetime.now(timezone.utc))
            async with self.db_manager.connection_scope() as conn:
                await conn.executemany(
                    "INSERT INTO recommendation_plan_pins (plan_id, recommendation_id, revision, pinned_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (plan_id, recommendation_id) DO NOTHING",
                    [(plan_id, int(rec_id), max(1, int(revision)), now) for rec_id, revision in pins.items()],
                )
                await conn.commit()
        return await self.get_plan_pins(plan_id)

    async def get_plan_pins(self, plan_id: str) -> Dict[int, int]:
        """``{recommendation_id: revision}`` pinned by a plan."""
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT recommendation_id, revision FROM recommendation_plan_pins WHERE plan_id = ?",
                (plan_id,),
            )
            rows = await cursor.fetchall()
        return {int(row["recommendation_id"]): int(row["revision"]) for row in rows}

    async def backfill_v2_fields(self, cluster_uid: str, batch_size: int = 500) -> int:
        """Fill the v2 columns of rows written before them from their flat columns (E4).

        Lossless: only NULL v2 columns are written, the flat columns are kept.
        Returns the number of rows filled.
        """
        filled = 0
        after_id = 0
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            while True:
                cursor = await conn.execute(
                    "SELECT * FROM recommendation_history WHERE fingerprint_v4 IS NULL AND id > ? ORDER BY id LIMIT ?",
                    (after_id, max(1, int(batch_size))),
                )
                rows = await cursor.fetchall()
                if not rows:
                    break
                after_id = int(rows[-1]["id"])
                updates = [update for update in (backfill_update(row, cluster_uid) for row in rows) if update]
                if updates:
                    await conn.executemany(
                        "UPDATE recommendation_history SET target = COALESCE(target, ?), "
                        "proposal = COALESCE(proposal, ?), fingerprint_v4 = ? "
                        "WHERE id = ? AND fingerprint_v4 IS NULL",
                        updates,
                    )
                    await conn.commit()
                    filled += len(updates)
        return filled

    async def reconcile_active_recommendations(
        self,
        records: List[RecommendationRecord],
        namespace: Optional[str] = None,
        incomplete_sources: Optional[Collection[str]] = None,
    ) -> int:
        """Marks active recommendations absent from the latest generated set as stale.

        Recommendations supported by a source listed in ``incomplete_sources``
        are kept active: a failed or partial source never causes staleness.
        """
        current_keys = {_identity_key(record) for record in records}
        blocked = frozenset(incomplete_sources or ())
        now = to_iso_z(datetime.now(timezone.utc))

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            params: list = []
            query = (
                "SELECT id, pod_name, container_name, namespace, type, scope, target_node, fingerprint, "
                "source, sources FROM recommendation_history WHERE status = 'active'"
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
                if _identity_key(row_record) in current_keys:
                    continue
                if not may_mark_stale(row["source"], sources_from_json(row["sources"]), blocked):
                    continue
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
        *,
        include_pr_open: bool = False,
    ) -> List[RecommendationRecord]:
        """Returns all currently active recommendations.

        Args:
            namespace: Optional namespace filter.
            include_pr_open: Also return recommendations whose pull request is
                open (``pr_open``), for listings that show them with their PR.

        Returns:
            A list of active RecommendationRecord objects.
        """
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            conditions = [_ACTIVE_LISTING_PREDICATE[bool(include_pr_open)]]
            params: list = []

            if namespace:
                conditions.append("namespace = ?")
                params.append(namespace)

            where = " AND ".join(conditions)
            query = f"SELECT * FROM recommendation_history WHERE {where} ORDER BY {ACTIVE_ORDER_BY}"
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

    async def transition_and_record_event(
        self,
        rec_id: int,
        updates: dict,
        event_type: str,
        actor: str,
        payload: dict,
        *,
        expected_statuses: Optional[List[str]] = None,
    ) -> Optional[RecommendationRecord]:
        """Atomically update a recommendation and append its lifecycle event.

        With ``expected_statuses`` the update is a compare-and-swap: nothing is
        written and ``None`` is returned when the stored status differs.
        """
        filtered = {k: v for k, v in updates.items() if k in MUTABLE_COLUMNS}
        if not filtered:
            raise ValueError("No mutable recommendation columns supplied.")
        now = datetime.now(timezone.utc)
        filtered.setdefault("updated_at", now)
        where = "id = ?"
        params = [_encode_value(value) for value in filtered.values()] + [rec_id]
        if expected_statuses is not None:
            where += f" AND status IN ({', '.join('?' for _ in expected_statuses)})"
            params.extend(_encode_value(status) for status in expected_statuses)
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            try:
                cursor = await conn.execute(
                    f"UPDATE recommendation_history SET {', '.join(f'{k} = ?' for k in filtered)} WHERE {where}",
                    params,
                )
                if expected_statuses is not None and cursor.rowcount != 1:
                    await conn.rollback()
                    return None
                await conn.execute(
                    """INSERT INTO recommendation_events
                    (recommendation_id, event_type, actor, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (rec_id, event_type, actor, json.dumps(payload, default=str), to_iso_z(now)),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
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
