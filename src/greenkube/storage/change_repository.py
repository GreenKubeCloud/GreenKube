# src/greenkube/storage/change_repository.py
"""Storage of changes, baselines and change measurements (C1, C2) on SQLite and PostgreSQL.

Tables (migrations 0056, 0057):

* ``change_events``, ``change_members``, ``baseline_snapshots``,
  ``change_measurements``, ``change_state_events`` — insert-only; database
  triggers reject UPDATE and DELETE;
* ``change_states`` — the current lifecycle state of each change; every
  transition is a compare-and-swap on ``(state, version)`` committed in the
  same transaction as its ``change_state_events`` row;
* ``workload_spec_observations`` — pod-template revisions seen by the applied
  detector (append-only).

Queries are written once with ``?`` placeholders (``?j`` for a JSON value) and
translated for PostgreSQL (``$n``, ``$n::jsonb``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from greenkube.utils.date_utils import ensure_utc, to_iso_z
from greenkube.verify.model import (
    BaselineSnapshot,
    ChangeEvent,
    ChangeMeasurement,
    ChangeStateEvent,
    ChangeStatus,
    MeasurementState,
    WorkloadRef,
    WorkloadSpecObservation,
    is_allowed_transition,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\?j?")

_CHANGE_TIMESTAMPS = ("synced_at", "rollout_started_at", "rollout_completed_at", "baseline_window_end", "created_at")
_STATUS_TIMESTAMPS = ("post_window_start", "post_window_end", "updated_at")


class ChangeConflictError(RuntimeError):
    """A compare-and-swap transition found a different state or version."""


class InvalidChangeTransitionError(ValueError):
    """The requested transition is not part of the change lifecycle (C2)."""


def change_identity(
    *,
    plan_id: Optional[str],
    workload_uid: Optional[str],
    target: Tuple[str, str, str],
    generation_after: Optional[int],
    spec_digest_after: Optional[str],
    rollout_completed_at: datetime,
) -> str:
    """Identity of a change: one plan (or none) rolling one revision out on one workload."""
    workload = workload_uid or "/".join(target)
    if generation_after is not None:
        revision = str(generation_after)
    else:
        revision = spec_digest_after or ensure_utc(rollout_completed_at).isoformat()
    return f"{plan_id or '-'}|{workload}|{revision}"


def dedupe_key(change: ChangeEvent) -> str:
    """:func:`change_identity` of a recorded change."""
    return change_identity(
        plan_id=change.plan_id,
        workload_uid=change.workload_uid,
        target=change.target.key,
        generation_after=change.generation_after,
        spec_digest_after=change.spec_digest_after,
        rollout_completed_at=change.rollout_completed_at,
    )


def _cluster_name() -> str:
    """The cluster name the ledger identity is stored under (``CLUSTER_NAME``, roadmap N9)."""
    from greenkube.core.savings_attributor import savings_cluster_name

    return savings_cluster_name()


def _loads(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _required_time(value: Any) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError("A required timestamp column is NULL.")
    return parsed


class ChangeRepository:
    """Changes, baseline snapshots, change measurements and their lifecycle."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    # ------------------------------------------------------------------
    # SQL dialect helpers
    # ------------------------------------------------------------------

    @property
    def _postgres(self) -> bool:
        return getattr(self.db_manager, "db_type", "sqlite") == "postgres"

    def _sql(self, query: str) -> str:
        if not self._postgres:
            return _PLACEHOLDER.sub("?", query)
        counter = iter(range(1, 10_000))

        def replace(match: re.Match) -> str:
            index = next(counter)
            return f"${index}::jsonb" if match.group(0) == "?j" else f"${index}"

        return _PLACEHOLDER.sub(replace, query)

    def _ts(self, value: Optional[datetime]) -> Any:
        if value is None:
            return None
        value = ensure_utc(value)
        return value if self._postgres else to_iso_z(value)

    async def _fetch(self, conn, query: str, params: Sequence[Any]) -> List[dict]:
        sql = self._sql(query)
        if self._postgres:
            rows = await conn.fetch(sql, *params)
            return [dict(row) for row in rows]
        cursor = await conn.execute(sql, list(params))
        rows = await cursor.fetchall()
        names = [column[0] for column in cursor.description or []]
        return [dict(zip(names, row)) for row in rows]

    async def _select(self, query: str, params: Sequence[Any] = ()) -> List[dict]:
        async with self.db_manager.connection_scope() as conn:
            return await self._fetch(conn, query, params)

    async def _execute(self, conn, query: str, params: Sequence[Any]) -> int:
        """Execute a write; returns the affected row count."""
        sql = self._sql(query)
        if self._postgres:
            status = await conn.execute(sql, *params)
            try:
                return int(str(status).rsplit(" ", 1)[-1])
            except ValueError:
                return 0
        cursor = await conn.execute(sql, list(params))
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Changes
    # ------------------------------------------------------------------

    async def find_change_id(self, key: str) -> Optional[str]:
        rows = await self._select("SELECT change_id FROM change_events WHERE dedupe_key = ?", [key])
        return rows[0]["change_id"] if rows else None

    async def insert_change(self, change: ChangeEvent, *, actor: str = "applied-detector") -> Tuple[ChangeEvent, bool]:
        """Insert a change with its members, initial ``applied`` state and event.

        Idempotent on the change identity (:func:`dedupe_key`): when the change
        already exists its stored version is returned (with ``change``'s
        recommendations added as members) and ``created`` is ``False``.
        """
        key = dedupe_key(change)
        now = ensure_utc(change.created_at)
        created = False
        async with self.db_manager.transaction() as conn:
            existing = await self._fetch(conn, "SELECT change_id FROM change_events WHERE dedupe_key = ?", [key])
            change_id = existing[0]["change_id"] if existing else change.change_id
            if not existing:
                await self._execute(
                    conn,
                    "INSERT INTO change_events (change_id, dedupe_key, plan_id, pr_url, merge_sha, gitops_revision, "
                    "synced_at, namespace, workload_kind, workload_name, containers, workload_uid, generation_before, "
                    "generation_after, spec_digest_before, spec_digest_after, rollout_started_at, "
                    "rollout_started_source, rollout_completed_at, rollout_completed_source, baseline_window_end, "
                    "action_kind, application_method, load_metric, details, created_at, created_by, "
                    "resources_digest_after, image_digest_after, cluster_uid) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?j, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?j, ?, ?, ?, ?, "
                    # N9: the cluster UID stored next to CLUSTER_NAME, once the controller resolved it.
                    "(SELECT cluster_uid FROM cluster_identities WHERE cluster_name = ?))",
                    [
                        change.change_id,
                        key,
                        change.plan_id,
                        change.pr_url,
                        change.merge_sha,
                        change.gitops_revision,
                        self._ts(change.synced_at),
                        change.target.namespace,
                        change.target.kind,
                        change.target.name,
                        _dumps(sorted(set(change.containers))),
                        change.workload_uid,
                        change.generation_before,
                        change.generation_after,
                        change.spec_digest_before,
                        change.spec_digest_after,
                        self._ts(change.rollout_started_at),
                        change.rollout_started_source,
                        self._ts(change.rollout_completed_at),
                        change.rollout_completed_source,
                        self._ts(change.baseline_window_end),
                        change.action_kind,
                        change.application_method.value,
                        change.load_metric,
                        _dumps(change.details),
                        self._ts(now),
                        change.created_by,
                        change.resources_digest_after,
                        change.image_digest_after,
                        _cluster_name(),
                    ],
                )
                await self._execute(
                    conn,
                    "INSERT INTO change_states (change_id, state, version, sequence, details, updated_at) "
                    "VALUES (?, ?, 1, 1, ?j, ?)",
                    [change.change_id, MeasurementState.APPLIED.value, _dumps({}), self._ts(now)],
                )
                await self._insert_event(
                    conn,
                    ChangeStateEvent(
                        change_id=change.change_id,
                        event_type="change_recorded",
                        to_state=MeasurementState.APPLIED,
                        actor=actor,
                        payload={"recommendation_ids": sorted(change.recommendation_ids), "dedupe_key": key},
                        created_at=now,
                    ),
                )
                created = True
            await self._add_members(conn, change_id, change.recommendation_ids, now)
        stored = await self.get_change(change_id)
        if stored is None:  # pragma: no cover - the row was just written or read
            raise RuntimeError(f"Change {change_id} disappeared after insert.")
        return stored, created

    async def _add_members(self, conn, change_id: str, recommendation_ids: Iterable[int], now: datetime) -> None:
        conflict = "ON CONFLICT (change_id, recommendation_id) DO NOTHING"
        for recommendation_id in sorted(set(recommendation_ids)):
            await self._execute(
                conn,
                f"INSERT INTO change_members (change_id, recommendation_id, added_at) VALUES (?, ?, ?) {conflict}",
                [change_id, int(recommendation_id), self._ts(now)],
            )

    async def add_members(self, change_id: str, recommendation_ids: Iterable[int], now: datetime) -> None:
        async with self.db_manager.transaction() as conn:
            await self._add_members(conn, change_id, recommendation_ids, now)

    def _change_from_row(self, row: dict, members: List[int]) -> ChangeEvent:
        values = dict(row)
        for column in _CHANGE_TIMESTAMPS:
            values[column] = _parse_time(values.get(column))
        return ChangeEvent(
            change_id=values["change_id"],
            recommendation_ids=members,
            plan_id=values.get("plan_id"),
            pr_url=values.get("pr_url"),
            merge_sha=values.get("merge_sha"),
            gitops_revision=values.get("gitops_revision"),
            synced_at=values.get("synced_at"),
            target=WorkloadRef(
                namespace=values["namespace"], kind=values["workload_kind"], name=values["workload_name"]
            ),
            containers=list(_loads(values.get("containers")) or []),
            workload_uid=values.get("workload_uid"),
            generation_before=values.get("generation_before"),
            generation_after=values.get("generation_after"),
            spec_digest_before=values.get("spec_digest_before"),
            spec_digest_after=values.get("spec_digest_after"),
            resources_digest_after=values.get("resources_digest_after"),
            image_digest_after=values.get("image_digest_after"),
            rollout_started_at=values["rollout_started_at"],
            rollout_started_source=values["rollout_started_source"],
            rollout_completed_at=values["rollout_completed_at"],
            rollout_completed_source=values["rollout_completed_source"],
            baseline_window_end=values["baseline_window_end"],
            action_kind=values["action_kind"],
            application_method=values["application_method"],
            load_metric=values.get("load_metric"),
            details=dict(_loads(values.get("details")) or {}),
            created_at=values["created_at"],
            created_by=values["created_by"],
        )

    async def _members(self, change_ids: Sequence[str]) -> Dict[str, List[int]]:
        if not change_ids:
            return {}
        placeholders = ", ".join("?" for _ in change_ids)
        rows = await self._select(
            f"SELECT change_id, recommendation_id FROM change_members WHERE change_id IN ({placeholders}) "
            "ORDER BY recommendation_id",
            list(change_ids),
        )
        members: Dict[str, List[int]] = {change_id: [] for change_id in change_ids}
        for row in rows:
            members.setdefault(row["change_id"], []).append(int(row["recommendation_id"]))
        return members

    async def _changes(self, rows: List[dict]) -> List[ChangeEvent]:
        members = await self._members([row["change_id"] for row in rows])
        return [self._change_from_row(row, members.get(row["change_id"], [])) for row in rows]

    async def get_change(self, change_id: str) -> Optional[ChangeEvent]:
        rows = await self._select("SELECT * FROM change_events WHERE change_id = ?", [change_id])
        changes = await self._changes(rows)
        return changes[0] if changes else None

    async def changes_for_workload(self, namespace: str, kind: str, name: str) -> List[ChangeEvent]:
        rows = await self._select(
            "SELECT * FROM change_events WHERE namespace = ? AND workload_kind = ? AND workload_name = ? "
            "ORDER BY rollout_completed_at, change_id",
            [namespace, kind, name],
        )
        return await self._changes(rows)

    async def changes_landed_between(self, start: datetime, end: datetime, *, limit: int = 200) -> List[ChangeEvent]:
        """Changes whose rollout completed in ``(start, end)``, oldest first (pool confounders, C4)."""
        rows = await self._select(
            "SELECT * FROM change_events WHERE rollout_completed_at > ? AND rollout_completed_at < ? "
            "ORDER BY rollout_completed_at, change_id LIMIT ?",
            [self._ts(start), self._ts(end), int(limit)],
        )
        return await self._changes(rows)

    async def change_ids_for_recommendations(self, recommendation_ids: Sequence[int]) -> Dict[int, str]:
        if not recommendation_ids:
            return {}
        placeholders = ", ".join("?" for _ in recommendation_ids)
        rows = await self._select(
            f"SELECT recommendation_id, change_id FROM change_members WHERE recommendation_id IN ({placeholders})",
            [int(item) for item in recommendation_ids],
        )
        return {int(row["recommendation_id"]): row["change_id"] for row in rows}

    async def list_changes(
        self,
        *,
        namespace: Optional[str] = None,
        states: Optional[Sequence[str]] = None,
        limit: int = 50,
        before: Optional[Tuple[datetime, str]] = None,
        plan_id: Optional[str] = None,
    ) -> List[Tuple[ChangeEvent, ChangeStatus]]:
        """Changes newest first, with their state. ``before`` is a ``(created_at, change_id)`` cursor.

        ``plan_id`` keeps the changes of one GitOps plan (the ledger deep link of a PR, J1).
        """
        clauses: List[str] = []
        params: List[Any] = []
        if namespace:
            clauses.append("e.namespace = ?")
            params.append(namespace)
        if plan_id:
            clauses.append("e.plan_id = ?")
            params.append(plan_id)
        if states:
            clauses.append(f"s.state IN ({', '.join('?' for _ in states)})")
            params.extend(states)
        if before is not None:
            clauses.append("(e.created_at < ? OR (e.created_at = ? AND e.change_id < ?))")
            params.extend([self._ts(before[0]), self._ts(before[0]), before[1]])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = await self._select(
            "SELECT e.*, s.state AS s_state, s.version AS s_version, s.sequence AS s_sequence, "
            "s.latest_measurement_id AS s_latest_measurement_id, s.post_window_start AS s_post_window_start, "
            "s.post_window_end AS s_post_window_end, s.details AS s_details, s.updated_at AS s_updated_at "
            f"FROM change_events e JOIN change_states s ON s.change_id = e.change_id {where} "
            "ORDER BY e.created_at DESC, e.change_id DESC LIMIT ?",
            params,
        )
        changes = await self._changes([{k: v for k, v in row.items() if not k.startswith("s_")} for row in rows])
        statuses = [
            self._status_from_row(
                {"change_id": row["change_id"], **{k[2:]: v for k, v in row.items() if k.startswith("s_")}}
            )
            for row in rows
        ]
        return list(zip(changes, statuses))

    # ------------------------------------------------------------------
    # Lifecycle state (C2)
    # ------------------------------------------------------------------

    def _status_from_row(self, row: dict) -> ChangeStatus:
        values = dict(row)
        for column in _STATUS_TIMESTAMPS:
            values[column] = _parse_time(values.get(column))
        return ChangeStatus(
            change_id=values["change_id"],
            state=MeasurementState(values["state"]),
            version=int(values["version"]),
            sequence=int(values["sequence"]),
            latest_measurement_id=values.get("latest_measurement_id"),
            post_window_start=values.get("post_window_start"),
            post_window_end=values.get("post_window_end"),
            details=dict(_loads(values.get("details")) or {}),
            updated_at=values["updated_at"],
        )

    async def get_status(self, change_id: str) -> Optional[ChangeStatus]:
        rows = await self._select("SELECT * FROM change_states WHERE change_id = ?", [change_id])
        return self._status_from_row(rows[0]) if rows else None

    async def list_statuses(self, states: Sequence[str]) -> List[ChangeStatus]:
        if not states:
            return []
        rows = await self._select(
            f"SELECT * FROM change_states WHERE state IN ({', '.join('?' for _ in states)}) ORDER BY change_id",
            list(states),
        )
        return [self._status_from_row(row) for row in rows]

    async def transition(
        self,
        current: ChangeStatus,
        to_state: MeasurementState,
        *,
        event_type: str,
        actor: str = "system",
        reason: str = "",
        payload: Optional[dict] = None,
        sequence: Optional[int] = None,
        latest_measurement_id: Optional[str] = None,
        post_window_start: Optional[datetime] = None,
        post_window_end: Optional[datetime] = None,
        details: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> ChangeStatus:
        """Compare-and-swap ``current`` to ``to_state`` and append its event, atomically.

        Raises :class:`ChangeConflictError` when the stored state or version
        differs from ``current`` (a concurrent transition won), and
        :class:`InvalidChangeTransitionError` when ``to_state`` cannot follow
        ``current.state`` in the lifecycle.
        """
        if not is_allowed_transition(current.state, to_state):
            raise InvalidChangeTransitionError(
                f"Change {current.change_id} cannot move from {current.state.value} to {to_state.value}."
            )
        moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        updated = current.model_copy(
            update={
                "state": to_state,
                "version": current.version + 1,
                "sequence": sequence if sequence is not None else current.sequence,
                "latest_measurement_id": latest_measurement_id or current.latest_measurement_id,
                "post_window_start": post_window_start or current.post_window_start,
                "post_window_end": post_window_end or current.post_window_end,
                "details": details if details is not None else current.details,
                "updated_at": moment,
            }
        )
        async with self.db_manager.transaction() as conn:
            count = await self._execute(
                conn,
                "UPDATE change_states SET state = ?, version = ?, sequence = ?, latest_measurement_id = ?, "
                "post_window_start = ?, post_window_end = ?, details = ?j, updated_at = ? "
                "WHERE change_id = ? AND state = ? AND version = ?",
                [
                    updated.state.value,
                    updated.version,
                    updated.sequence,
                    updated.latest_measurement_id,
                    self._ts(updated.post_window_start),
                    self._ts(updated.post_window_end),
                    _dumps(updated.details),
                    self._ts(moment),
                    current.change_id,
                    current.state.value,
                    current.version,
                ],
            )
            if count != 1:
                raise ChangeConflictError(
                    f"Change {current.change_id} is no longer {current.state.value} (version {current.version})."
                )
            await self._insert_event(
                conn,
                ChangeStateEvent(
                    change_id=current.change_id,
                    event_type=event_type,
                    from_state=current.state,
                    to_state=to_state,
                    actor=actor,
                    reason=reason,
                    payload=payload or {},
                    created_at=moment,
                ),
            )
        return updated

    async def _insert_event(self, conn, event: ChangeStateEvent) -> None:
        await self._execute(
            conn,
            "INSERT INTO change_state_events (change_id, event_type, from_state, to_state, actor, reason, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?j, ?)",
            [
                event.change_id,
                event.event_type,
                event.from_state.value if event.from_state is not None else None,
                event.to_state.value if event.to_state is not None else None,
                event.actor,
                event.reason,
                _dumps(event.payload),
                self._ts(event.created_at),
            ],
        )

    async def record_event(
        self,
        change_id: str,
        event_type: str,
        *,
        actor: str = "system",
        reason: str = "",
        payload: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Append an event that does not change the state (e.g. ``savings_reversed``, C8)."""
        moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        async with self.db_manager.transaction() as conn:
            await self._insert_event(
                conn,
                ChangeStateEvent(
                    change_id=change_id,
                    event_type=event_type,
                    actor=actor,
                    reason=reason,
                    payload=payload or {},
                    created_at=moment,
                ),
            )

    async def list_events(self, change_id: str) -> List[ChangeStateEvent]:
        rows = await self._select(
            "SELECT * FROM change_state_events WHERE change_id = ? ORDER BY id ASC",
            [change_id],
        )
        return [
            ChangeStateEvent(
                id=int(row["id"]),
                change_id=row["change_id"],
                event_type=row["event_type"],
                from_state=row.get("from_state"),
                to_state=row.get("to_state"),
                actor=row["actor"],
                reason=row.get("reason") or "",
                payload=dict(_loads(row.get("payload")) or {}),
                created_at=_required_time(row["created_at"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Baseline snapshots
    # ------------------------------------------------------------------

    async def insert_snapshot(self, snapshot: BaselineSnapshot) -> BaselineSnapshot:
        """Persist a snapshot; a change keeps the first snapshot written for it."""
        conflict = "ON CONFLICT (change_id) DO NOTHING"
        async with self.db_manager.transaction() as conn:
            await self._execute(
                conn,
                "INSERT INTO baseline_snapshots (snapshot_id, change_id, content_sha256, window_start, window_end, "
                "hours, load_series, price_card, intensity_source, impact_model_version, collector_versions, "
                f"created_at, health) VALUES (?, ?, ?, ?, ?, ?j, ?j, ?j, ?, ?, ?j, ?, ?j) {conflict}",
                [
                    snapshot.snapshot_id,
                    snapshot.change_id,
                    snapshot.content_sha256,
                    self._ts(snapshot.window.start),
                    self._ts(snapshot.window.end),
                    _dumps([bucket.model_dump(mode="json") for bucket in snapshot.hours]),
                    _dumps(snapshot.load_series),
                    _dumps(snapshot.price_card),
                    snapshot.intensity_source,
                    snapshot.impact_model_version,
                    _dumps(snapshot.collector_versions),
                    self._ts(snapshot.created_at),
                    _dumps(snapshot.health),
                ],
            )
        stored = await self.get_snapshot_for_change(snapshot.change_id)
        if stored is None:  # pragma: no cover
            raise RuntimeError(f"Baseline snapshot of change {snapshot.change_id} disappeared after insert.")
        return stored

    def _snapshot_from_row(self, row: dict) -> BaselineSnapshot:
        return BaselineSnapshot.model_validate(
            {
                "snapshot_id": row["snapshot_id"],
                "change_id": row["change_id"],
                "content_sha256": row["content_sha256"],
                "window": {"start": _parse_time(row["window_start"]), "end": _parse_time(row["window_end"])},
                "hours": _loads(row.get("hours")) or [],
                "load_series": _loads(row.get("load_series")) or {},
                "price_card": _loads(row.get("price_card")) or {},
                "intensity_source": row.get("intensity_source"),
                "impact_model_version": row.get("impact_model_version"),
                "collector_versions": _loads(row.get("collector_versions")) or {},
                "health": _loads(row.get("health")) or {},
                "created_at": _parse_time(row["created_at"]),
            }
        )

    async def get_snapshot_for_change(self, change_id: str) -> Optional[BaselineSnapshot]:
        rows = await self._select("SELECT * FROM baseline_snapshots WHERE change_id = ?", [change_id])
        return self._snapshot_from_row(rows[0]) if rows else None

    # ------------------------------------------------------------------
    # Change measurements
    # ------------------------------------------------------------------

    async def insert_measurement(self, measurement: ChangeMeasurement) -> Tuple[ChangeMeasurement, bool]:
        """Persist a decided measurement; returns the stored one for its cycle and whether it was new."""
        data = measurement.model_dump(mode="json")
        conflict = "ON CONFLICT (change_id, sequence) DO NOTHING"
        async with self.db_manager.transaction() as conn:
            count = await self._execute(
                conn,
                "INSERT INTO change_measurements (measurement_id, change_id, sequence, strategy_key, strategy_version, "
                "impact_model_version, baseline_snapshot_id, baseline_sha256, windows, load_driver, "
                "counterfactual_params, estimates, confounders, confidence, health, allocations, outcome, reasons, "
                "evidence_sha256, supersedes_measurement_id, decided_at, capacity) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?j, ?j, ?j, ?j, ?j, ?, ?j, ?j, ?, ?j, ?, ?, ?, ?j) {conflict}",
                [
                    measurement.measurement_id,
                    measurement.change_id,
                    measurement.sequence,
                    measurement.strategy_key,
                    measurement.strategy_version,
                    measurement.impact_model_version,
                    measurement.baseline_snapshot_id,
                    measurement.baseline_sha256,
                    _dumps(data["windows"]),
                    _dumps(data["load_driver"]) if data["load_driver"] is not None else None,
                    _dumps(data["counterfactual_params"]),
                    _dumps(data["estimates"]),
                    _dumps(data["confounders"]),
                    measurement.confidence,
                    _dumps(data["health"]),
                    _dumps(data["allocations"]),
                    measurement.outcome.value,
                    _dumps(data["reasons"]),
                    measurement.evidence_sha256,
                    measurement.supersedes_measurement_id,
                    self._ts(measurement.decided_at),
                    _dumps(data["capacity"]) if data["capacity"] is not None else None,
                ],
            )
        stored = await self.get_measurement_for_cycle(measurement.change_id, measurement.sequence)
        if stored is None:  # pragma: no cover
            raise RuntimeError(f"Measurement of change {measurement.change_id} disappeared after insert.")
        return stored, count == 1

    def _measurement_from_row(self, row: dict) -> ChangeMeasurement:
        values = dict(row)
        for column in (
            "windows",
            "load_driver",
            "counterfactual_params",
            "estimates",
            "confounders",
            "health",
            "allocations",
            "reasons",
            "capacity",
        ):
            values[column] = _loads(values.get(column))
        values["decided_at"] = _parse_time(values.get("decided_at"))
        if values.get("counterfactual_params") is None:
            values["counterfactual_params"] = {}
        return ChangeMeasurement.model_validate(values)

    async def get_measurement_for_cycle(self, change_id: str, sequence: int) -> Optional[ChangeMeasurement]:
        rows = await self._select(
            "SELECT * FROM change_measurements WHERE change_id = ? AND sequence = ?", [change_id, int(sequence)]
        )
        return self._measurement_from_row(rows[0]) if rows else None

    async def list_measurements(self, change_id: str) -> List[ChangeMeasurement]:
        rows = await self._select(
            "SELECT * FROM change_measurements WHERE change_id = ? ORDER BY sequence ASC", [change_id]
        )
        return [self._measurement_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Workload spec observations
    # ------------------------------------------------------------------

    def _observation_from_row(self, row: dict) -> WorkloadSpecObservation:
        return WorkloadSpecObservation(
            namespace=row["namespace"],
            kind=row["workload_kind"],
            name=row["workload_name"],
            workload_uid=row.get("workload_uid"),
            generation=row.get("generation"),
            spec_digest=row.get("spec_digest"),
            hpa_digest=row.get("hpa_digest"),
            resources_digest=row.get("resources_digest"),
            image_digest=row.get("image_digest"),
            container_resources=_loads(row.get("container_resources")) or None,
            observed_at=_required_time(row["observed_at"]),
        )

    async def latest_spec_observations(self) -> Dict[Tuple[str, str, str], WorkloadSpecObservation]:
        """Latest observation of every workload."""
        rows = await self._select(
            "SELECT o.* FROM workload_spec_observations o JOIN ("
            "SELECT namespace, workload_kind, workload_name, MAX(id) AS max_id FROM workload_spec_observations "
            "GROUP BY namespace, workload_kind, workload_name) latest ON latest.max_id = o.id"
        )
        observations = [self._observation_from_row(row) for row in rows]
        return {item.key: item for item in observations}

    async def insert_spec_observations(self, observations: Sequence[WorkloadSpecObservation]) -> int:
        if not observations:
            return 0
        async with self.db_manager.transaction() as conn:
            for item in observations:
                await self._execute(
                    conn,
                    "INSERT INTO workload_spec_observations (namespace, workload_kind, workload_name, workload_uid, "
                    "generation, spec_digest, hpa_digest, resources_digest, image_digest, container_resources, "
                    "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?j, ?)",
                    [
                        item.namespace,
                        item.kind,
                        item.name,
                        item.workload_uid,
                        item.generation,
                        item.spec_digest,
                        item.hpa_digest,
                        item.resources_digest,
                        item.image_digest,
                        _dumps(item.container_resources) if item.container_resources is not None else None,
                        self._ts(item.observed_at),
                    ],
                )
        return len(observations)

    async def spec_observations(
        self, namespace: str, kind: str, name: str, *, since: Optional[datetime] = None
    ) -> List[WorkloadSpecObservation]:
        """Observations of one workload, oldest first (the last one before ``since`` included)."""
        workload = (
            "SELECT * FROM workload_spec_observations WHERE namespace = ? AND workload_kind = ? AND workload_name = ?"
        )
        if since is None:
            rows = await self._select(f"{workload} ORDER BY id ASC", [namespace, kind, name])
            return [self._observation_from_row(row) for row in rows]
        threshold = self._ts(ensure_utc(since))
        earlier = await self._select(
            f"{workload} AND observed_at < ? ORDER BY id DESC LIMIT 1", [namespace, kind, name, threshold]
        )
        later = await self._select(
            f"{workload} AND observed_at >= ? ORDER BY id ASC", [namespace, kind, name, threshold]
        )
        return [self._observation_from_row(row) for row in [*earlier, *later]]


def get_change_repository(db_manager=None) -> ChangeRepository:
    """Return a change repository bound to ``db_manager`` (the shared manager by default)."""
    if db_manager is None:
        from greenkube.core.db import get_db_manager

        db_manager = get_db_manager()
    return ChangeRepository(db_manager)
