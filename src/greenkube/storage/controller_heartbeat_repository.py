# src/greenkube/storage/controller_heartbeat_repository.py
"""Persisted controller loop heartbeats (migration 0076) on SQLite and PostgreSQL.

Every controller replica writes one row: whether it holds the controller
Lease, whether all its loops are ready, and the per-loop snapshot of its
:class:`~greenkube.core.loops.LoopRegistry`. The API process reads the rows to
report controller loop state in ``GET /api/v1/automation/status``: the loops
run in another process, so their in-memory state is not visible to the API.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from greenkube.utils.date_utils import ensure_utc, to_iso_z

_PLACEHOLDER = re.compile(r"\?")


@dataclass(frozen=True)
class ControllerHeartbeat:
    """The last published state of one controller replica."""

    instance_id: str
    role: str
    leader: bool
    ready: bool
    heartbeat_at: datetime
    started_at: datetime
    loops: List[Dict[str, Any]] = field(default_factory=list)

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (ensure_utc(now) - ensure_utc(self.heartbeat_at)).total_seconds())


class ControllerHeartbeatRepository:
    """Upserts and reads ``controller_heartbeats`` rows."""

    def __init__(self, db_manager: Any):
        self.db_manager = db_manager

    @property
    def _postgres(self) -> bool:
        return getattr(self.db_manager, "db_type", "sqlite") == "postgres"

    def _sql(self, query: str) -> str:
        if not self._postgres:
            return query
        counter = iter(range(1, 10_000))
        return _PLACEHOLDER.sub(lambda _match: f"${next(counter)}", query)

    def _ts(self, value: datetime) -> Any:
        value = ensure_utc(value)
        return value if self._postgres else to_iso_z(value)

    async def _execute(self, query: str, params: Sequence[Any]) -> None:
        sql = self._sql(query)
        async with self.db_manager.transaction() as conn:
            if self._postgres:
                await conn.execute(sql, *params)
            else:
                await conn.execute(sql, list(params))

    async def _fetch(self, query: str, params: Sequence[Any]) -> List[dict]:
        sql = self._sql(query)
        async with self.db_manager.connection_scope() as conn:
            if self._postgres:
                return [dict(row) for row in await conn.fetch(sql, *params)]
            cursor = await conn.execute(sql, list(params))
            rows = await cursor.fetchall()
            names = [column[0] for column in cursor.description or []]
            return [dict(zip(names, row)) for row in rows]

    async def publish(self, heartbeat: ControllerHeartbeat) -> None:
        """Insert or replace the row of ``heartbeat.instance_id``."""
        loops = json.dumps(heartbeat.loops, default=str, separators=(",", ":"))
        loops_param = "?::jsonb" if self._postgres else "?"
        await self._execute(
            "INSERT INTO controller_heartbeats (instance_id, role, leader, ready, heartbeat_at, started_at, loops) "
            f"VALUES (?, ?, ?, ?, ?, ?, {loops_param}) "
            "ON CONFLICT (instance_id) DO UPDATE SET role = excluded.role, leader = excluded.leader, "
            "ready = excluded.ready, heartbeat_at = excluded.heartbeat_at, started_at = excluded.started_at, "
            "loops = excluded.loops",
            [
                heartbeat.instance_id,
                heartbeat.role,
                bool(heartbeat.leader),
                bool(heartbeat.ready),
                self._ts(heartbeat.heartbeat_at),
                self._ts(heartbeat.started_at),
                loops,
            ],
        )

    async def list_since(self, since: datetime) -> List[ControllerHeartbeat]:
        """Heartbeats written at or after ``since``, newest first."""
        cutoff = "heartbeat_at >= ?" if self._postgres else "julianday(heartbeat_at) >= julianday(?)"
        rows = await self._fetch(
            f"SELECT * FROM controller_heartbeats WHERE {cutoff} ORDER BY heartbeat_at DESC, instance_id",
            [self._ts(since)],
        )
        return [_row(row) for row in rows]

    async def prune_before(self, cutoff: datetime) -> None:
        """Delete the rows of replicas silent since before ``cutoff`` (replaced pods)."""
        where = "heartbeat_at < ?" if self._postgres else "julianday(heartbeat_at) < julianday(?)"
        await self._execute(f"DELETE FROM controller_heartbeats WHERE {where}", [self._ts(cutoff)])


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _loops(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _row(row: Dict[str, Any]) -> ControllerHeartbeat:
    return ControllerHeartbeat(
        instance_id=str(row["instance_id"]),
        role=str(row["role"]),
        leader=bool(row["leader"]),
        ready=bool(row["ready"]),
        heartbeat_at=_time(row["heartbeat_at"]),
        started_at=_time(row["started_at"]),
        loops=_loops(row["loops"]),
    )


def get_controller_heartbeat_repository(db_manager: Optional[Any] = None) -> ControllerHeartbeatRepository:
    if db_manager is None:
        from greenkube.core.db import get_db_manager

        db_manager = get_db_manager()
    return ControllerHeartbeatRepository(db_manager)
