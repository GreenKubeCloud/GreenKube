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
from typing import Collection, Dict, List, Mapping, Optional

from ...core.recommendation_ranking import normalize_savings_metric
from ...core.recommendation_realization import (
    RECORDED_CHANGE_STATUSES,
    absorbs_regenerated_recommendation,
    estimate_realized_savings,
    is_rightsizing_type,
    refresh_applied_recommendation,
)
from ...core.recommendation_reconciliation import may_mark_stale
from ...models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationEvent,
    RecommendationRecord,
    RecommendationSavingsSummary,
    RecommendationStatus,
    RecommendationType,
)
from ...models.recommendation_v2 import RecommendationRevision
from ..base_repository import RecommendationRepository
from ..recommendation_mapper import (
    ACTIVE_UPSERT_COLUMNS,
    RECORD_COLUMNS,
    record_values,
    row_to_record,
    sources_from_json,
    to_json,
)
from ..recommendation_revisions import (
    NO_OPEN_PLAN_SQL,
    QUEUED,
    SUPERSEDED,
    RevisionDecision,
    backfill_update,
    decide,
    is_frozen,
    prune_superseded_sql,
    revision_from_row,
    revision_values,
)
from ..sqlite.recommendation_repository import MUTABLE_COLUMNS

logger = logging.getLogger(__name__)

#: Status predicate of the active listing, optionally with recommendations whose PR is open.
_ACTIVE_LISTING_PREDICATE = {False: "status = 'active'", True: "status IN ('active', 'pr_open')"}

#: Default order of active lists (E2, BUG-20): ranking score, then projected
#: cost savings, NULLs last on both backends, then a stable id tie-break.
ACTIVE_ORDER_BY = "ranking_score DESC NULLS LAST, potential_savings_cost DESC NULLS LAST, id ASC"


def _encode_value(value):
    """Encodes a Python value for an asyncpg bind parameter."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            default=lambda item: (
                item.model_dump(mode="json")
                if hasattr(item, "model_dump")
                else item.isoformat()
                if hasattr(item, "isoformat")
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

        now = datetime.now(timezone.utc)
        set_columns = ACTIVE_UPSERT_COLUMNS + ("revision", "updated_at")
        update_query = "UPDATE recommendation_history SET {} WHERE id = ${} AND status = 'active'".format(
            ", ".join(f"{column} = ${index}" for index, column in enumerate(set_columns, start=1)),
            len(set_columns) + 1,
        )
        active_select_query = (
            "SELECT * FROM recommendation_history "
            "WHERE fingerprint = $1 "
            "AND COALESCE(scope, 'pod') = $2 "
            "AND namespace IS NOT DISTINCT FROM $3 "
            "AND pod_name IS NOT DISTINCT FROM $4 "
            "AND container_name IS NOT DISTINCT FROM $5 "
            "AND target_node IS NOT DISTINCT FROM $6 "
            "AND type = $7 "
            "AND status = 'active' "
            "ORDER BY id LIMIT 1"
        )
        insert_query = ("INSERT INTO recommendation_history ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
            ", ".join(RECORD_COLUMNS),
            ", ".join(f"${index}" for index in range(1, len(RECORD_COLUMNS) + 1)),
        )
        async with self.db_manager.connection_scope() as conn:
            # A PR in flight wins over an older applied row of the same target.
            applied_select_query = """
                SELECT * FROM recommendation_history
                WHERE COALESCE(scope, 'pod') = $1
                  AND namespace IS NOT DISTINCT FROM $2
                  AND pod_name IS NOT DISTINCT FROM $3
                  AND container_name IS NOT DISTINCT FROM $4
                  AND target_node IS NOT DISTINCT FROM $5
                  AND type = $6
                  AND (status = 'pr_open' OR status = ANY($7::text[]))
                ORDER BY (status = 'pr_open') DESC, applied_at DESC NULLS LAST, id DESC
                LIMIT 1
            """
            applied_update_query = """
                UPDATE recommendation_history SET
                    carbon_saved_co2e_grams = $1,
                    cost_saved = $2,
                    updated_at = $3
                WHERE id = $4 AND status = 'applied'
            """
            count = 0
            for r in records:
                type_val = _type_value(r)
                status_val = _status_value(r)

                values = record_values(r)
                existing = await conn.fetchrow(
                    active_select_query,
                    r.fingerprint,
                    r.scope or "pod",
                    r.namespace,
                    r.pod_name,
                    r.container_name,
                    r.target_node,
                    type_val,
                )
                if existing is not None:
                    stored = row_to_record(existing)
                    decision = decide(stored, r, frozen=is_frozen(existing["status"], existing["action_status"]))
                    if decision.action == "revise":
                        update_params = [values[column] for column in ACTIVE_UPSERT_COLUMNS] + [
                            decision.revision,
                            now,
                            existing["id"],
                        ]
                        async with conn.transaction():
                            # Guarded: a plan opened since the read freezes the record (E7).
                            result = await conn.execute(f"{update_query} AND {NO_OPEN_PLAN_SQL}", *update_params)
                            revised = not str(result).endswith(" 0")
                            if revised:
                                await self._settle_revision(conn, stored, True, now)
                        if revised:
                            count += 1
                            continue
                        decision = RevisionDecision("queue", decision.revision)
                    if decision.action == "refresh":
                        update_params = [values[column] for column in ACTIVE_UPSERT_COLUMNS] + [
                            decision.revision,
                            now,
                            existing["id"],
                        ]
                        async with conn.transaction():
                            await self._settle_revision(conn, stored, False, now)
                            # Guarded like a revision: a plan opened since the read freezes the record (E7).
                            result = await conn.execute(f"{update_query} AND {NO_OPEN_PLAN_SQL}", *update_params)
                        if not str(result).endswith(" 0"):
                            count += 1
                            continue
                        decision = RevisionDecision("keep", decision.revision)
                    if decision.action == "queue":
                        await self._queue_revision(conn, stored, r, decision.revision, now)
                    else:
                        await self._settle_revision(conn, stored, False, now)
                    # A record never changes under an open plan (E7), not even its shared action, but the
                    # scan still saw its target: keep it alive so it does not expire while the plan is open.
                    await conn.execute(
                        "UPDATE recommendation_history SET expires_at = COALESCE($1, expires_at), updated_at = $2 "
                        "WHERE id = $3",
                        values["expires_at"],
                        now,
                        existing["id"],
                    )
                    count += 1
                    continue

                applied_row = await conn.fetchrow(
                    applied_select_query,
                    r.scope or "pod",
                    r.namespace,
                    r.pod_name,
                    r.container_name,
                    r.target_node,
                    type_val,
                    list(RECORDED_CHANGE_STATUSES),
                )
                if applied_row and applied_row["status"] == RecommendationStatus.PR_OPEN.value:
                    # Frozen while its pull request is open (or merged and syncing): newer advice is queued.
                    stored = row_to_record(applied_row)
                    decision = decide(stored, r, frozen=True)
                    if decision.action == "queue":
                        await self._queue_revision(conn, stored, r, decision.revision, now)
                    else:
                        await self._settle_revision(conn, stored, False, now)
                    count += 1
                    continue
                if (
                    applied_row
                    and applied_row["status"] == RecommendationStatus.APPLIED.value
                    and is_rightsizing_type(applied_row["type"])
                    and not absorbs_regenerated_recommendation(row_to_record(applied_row), r, now)
                ):
                    # REC-01: an applied rightsizing only absorbs a restatement of its change.
                    logger.info(
                        "New %s advice for %s/%s after applied recommendation %s: inserted.",
                        applied_row["type"],
                        r.namespace,
                        r.pod_name,
                        applied_row["id"],
                    )
                    applied_row = None
                if applied_row and applied_row["status"] != RecommendationStatus.APPLIED.value:
                    if absorbs_regenerated_recommendation(row_to_record(applied_row), r, now):
                        # The scan restates a change already recorded (lagging rollup):
                        # never offer it again nor record it as a second change.
                        continue
                    applied_row = None
                if applied_row:
                    refreshed = refresh_applied_recommendation(row_to_record(applied_row), r, observed_at=now)
                    await conn.execute(
                        applied_update_query,
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
                insert_result = await conn.execute(insert_query, *(values[column] for column in RECORD_COLUMNS))
                if insert_result == "INSERT 0 0":
                    # Lost a race with a concurrent upsert; the row already exists.
                    continue
                count += 1

            logger.info("Upserted %d recommendation records in PostgreSQL.", count)
            return count

    @staticmethod
    async def _queue_revision(
        conn, stored: RecommendationRecord, incoming: RecommendationRecord, revision: int, now: datetime
    ) -> None:
        """Keep newer advice behind an open plan as the (single) queued revision."""
        values = revision_values(incoming)
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM recommendation_revisions WHERE recommendation_id = $1 AND state = $2 AND revision <> $3",
                stored.id,
                QUEUED,
                revision,
            )
            await conn.execute(
                """
                INSERT INTO recommendation_revisions
                    (recommendation_id, revision, state, proposal, consensus, snapshot, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
                ON CONFLICT (recommendation_id, revision) DO UPDATE SET
                    state = EXCLUDED.state,
                    proposal = EXCLUDED.proposal,
                    consensus = EXCLUDED.consensus,
                    snapshot = EXCLUDED.snapshot,
                    updated_at = EXCLUDED.updated_at
                """,
                stored.id,
                revision,
                QUEUED,
                to_json(values["proposal"]),
                to_json(values["consensus"]),
                to_json(values["snapshot"]),
                now,
            )

    @staticmethod
    async def _settle_revision(conn, stored: RecommendationRecord, revise: bool, now: datetime) -> None:
        """Drop queued advice; on a new revision keep the replaced proposal as ``superseded``."""
        await conn.execute(
            "DELETE FROM recommendation_revisions WHERE recommendation_id = $1 AND state = $2",
            stored.id,
            QUEUED,
        )
        if not revise:
            return
        values = revision_values(stored)
        await conn.execute(
            """
            INSERT INTO recommendation_revisions
                (recommendation_id, revision, state, proposal, consensus, snapshot, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (recommendation_id, revision) DO NOTHING
            """,
            stored.id,
            max(1, stored.revision),
            SUPERSEDED,
            to_json(values["proposal"]),
            to_json(values["consensus"]),
            to_json(values["snapshot"]),
            stored.updated_at or stored.created_at or now,
            now,
        )
        await conn.execute(prune_superseded_sql(lambda index: f"${index}"), stored.id, stored.id, stored.id)

    async def get_revisions(self, rec_id: int) -> List[RecommendationRevision]:
        """Revisions the record no longer carries (superseded and queued), newest first (E7)."""
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recommendation_revisions WHERE recommendation_id = $1 ORDER BY revision DESC",
                rec_id,
            )
        return [revision_from_row(row) for row in rows]

    async def get_queued_revisions(self, rec_ids: Collection[int]) -> Dict[int, RecommendationRevision]:
        """The queued revision of each record, when one waits behind an open plan."""
        ids = sorted({int(rec_id) for rec_id in rec_ids})
        if not ids:
            return {}
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recommendation_revisions WHERE state = $1 AND recommendation_id = ANY($2::bigint[])",
                QUEUED,
                ids,
            )
        return {revision.recommendation_id: revision for revision in map(revision_from_row, rows)}

    async def pin_plan_revisions(self, plan_id: str, pins: Mapping[int, int]) -> Dict[int, int]:
        """Record the revision a plan implements per recommendation; the first pin wins (E7)."""
        if pins:
            now = datetime.now(timezone.utc)
            async with self.db_manager.connection_scope() as conn:
                await conn.executemany(
                    "INSERT INTO recommendation_plan_pins (plan_id, recommendation_id, revision, pinned_at) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (plan_id, recommendation_id) DO NOTHING",
                    [(plan_id, int(rec_id), max(1, int(revision)), now) for rec_id, revision in pins.items()],
                )
        return await self.get_plan_pins(plan_id)

    async def get_plan_pins(self, plan_id: str) -> Dict[int, int]:
        """``{recommendation_id: revision}`` pinned by a plan."""
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT recommendation_id, revision FROM recommendation_plan_pins WHERE plan_id = $1",
                plan_id,
            )
        return {int(row["recommendation_id"]): int(row["revision"]) for row in rows}

    async def backfill_v2_fields(self, cluster_uid: str, batch_size: int = 500) -> int:
        """Fill the v2 columns of rows written before them from their flat columns (E4).

        Lossless: only NULL v2 columns are written, the flat columns are kept.
        Returns the number of rows filled.
        """
        filled = 0
        after_id = 0
        async with self.db_manager.connection_scope() as conn:
            while True:
                rows = await conn.fetch(
                    "SELECT * FROM recommendation_history WHERE fingerprint_v4 IS NULL AND id > $1 "
                    "ORDER BY id LIMIT $2",
                    after_id,
                    max(1, int(batch_size)),
                )
                if not rows:
                    break
                after_id = int(rows[-1]["id"])
                updates = [update for update in (backfill_update(row, cluster_uid) for row in rows) if update]
                if updates:
                    await conn.executemany(
                        "UPDATE recommendation_history SET target = COALESCE(target, $1), "
                        "proposal = COALESCE(proposal, $2), fingerprint_v4 = $3 "
                        "WHERE id = $4 AND fingerprint_v4 IS NULL",
                        updates,
                    )
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
        now = datetime.now(timezone.utc)

        async with self.db_manager.connection_scope() as conn:
            params: list = []
            query = (
                "SELECT id, pod_name, container_name, namespace, type, scope, target_node, fingerprint, "
                "source, sources FROM recommendation_history WHERE status = 'active'"
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
                    container_name=data["container_name"],
                    namespace=data["namespace"],
                    type=RecommendationType(data["type"]),
                    scope=data.get("scope") or "pod",
                    target_node=data.get("target_node"),
                    fingerprint=data.get("fingerprint"),
                    description="placeholder",
                )
                if _identity_key(row_record) in current_keys:
                    continue
                if not may_mark_stale(data.get("source"), sources_from_json(data.get("sources")), blocked):
                    continue
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
            params: list = []
            idx = 1
            conditions = [_ACTIVE_LISTING_PREDICATE[bool(include_pr_open)]]

            if namespace:
                conditions.append(f"namespace = ${idx}")
                params.append(namespace)

            where = " AND ".join(conditions)
            query = f"SELECT * FROM recommendation_history WHERE {where} ORDER BY {ACTIVE_ORDER_BY}"
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
                json.dumps(baseline.model_dump(mode="json") if hasattr(baseline, "model_dump") else baseline)
                if baseline is not None
                else None,
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
                json.dumps(
                    event.payload.model_dump(mode="json")
                    if hasattr(event.payload, "model_dump")
                    else event.payload or {}
                ),
                created_at,
            )
            return event.model_copy(update={"id": row["id"], "created_at": row["created_at"]})

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
        columns = list(filtered)
        assignments = ", ".join(f"{column} = ${index}" for index, column in enumerate(columns, 1))
        params: list = [_encode_value(filtered[column]) for column in columns]
        params.append(rec_id)
        where = f"id = ${len(params)}"
        if expected_statuses is not None:
            params.append([_encode_value(status) for status in expected_statuses])
            where += f" AND status = ANY(${len(params)}::text[])"
        async with self.db_manager.connection_scope() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"UPDATE recommendation_history SET {assignments} WHERE {where} RETURNING *",
                    *params,
                )
                if row is None:
                    if expected_statuses is not None:
                        return None
                    raise ValueError(f"Recommendation {rec_id} not found.")
                await conn.execute(
                    """INSERT INTO recommendation_events
                    (recommendation_id, event_type, actor, payload, created_at)
                    VALUES ($1, $2, $3, $4, $5)""",
                    rec_id,
                    event_type,
                    actor,
                    json.dumps(payload, default=str),
                    now,
                )
            return row_to_record(row)

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
