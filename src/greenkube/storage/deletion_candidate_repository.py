# src/greenkube/storage/deletion_candidate_repository.py
"""First-seen persistence of deletion candidates (migration 0063) on SQLite and PostgreSQL.

A LoadBalancer Service or PersistentVolume is recommended for deletion only
after it has looked unused continuously for ``DELETION_MIN_UNUSED_DAYS``.
Each complete evaluation of a resource kind calls :meth:`observe` with the
candidates of that evaluation, in one transaction:

* a candidate seen before keeps its ``first_seen_at``, unless its uid changed
  (the object was re-created) or it was not observed for longer than
  ``max_gap`` (GreenKube could not watch it, so continuity is unproven);
* resources of the kind that are no longer candidates are removed, so the
  clock restarts if they look unused again later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from greenkube.utils.date_utils import ensure_utc, to_iso_z

_PLACEHOLDER = re.compile(r"\?")

#: Longest interval between two observations that still counts as continuous.
DEFAULT_MAX_GAP = timedelta(hours=48)


@dataclass(frozen=True)
class CandidateObservation:
    """One resource that looked unused in this evaluation."""

    namespace: str
    name: str
    uid: str = ""
    evidence: List[str] = field(default_factory=list)


class DeletionCandidateRepository:
    """Stores since when each deletion candidate has looked unused."""

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

    async def _fetch(self, conn: Any, query: str, params: Sequence[Any]) -> List[dict]:
        sql = self._sql(query)
        if self._postgres:
            return [dict(row) for row in await conn.fetch(sql, *params)]
        cursor = await conn.execute(sql, list(params))
        rows = await cursor.fetchall()
        names = [column[0] for column in cursor.description or []]
        return [dict(zip(names, row)) for row in rows]

    async def _execute(self, conn: Any, query: str, params: Sequence[Any]) -> None:
        sql = self._sql(query)
        if self._postgres:
            await conn.execute(sql, *params)
        else:
            await conn.execute(sql, list(params))

    async def observe(
        self,
        resource_kind: str,
        candidates: Sequence[CandidateObservation],
        now: Optional[datetime] = None,
        max_gap: timedelta = DEFAULT_MAX_GAP,
    ) -> Dict[Tuple[str, str], datetime]:
        """Record a complete evaluation of ``resource_kind``; returns ``first_seen_at`` per (namespace, name)."""
        now = ensure_utc(now or datetime.now(timezone.utc))
        result: Dict[Tuple[str, str], datetime] = {}
        async with self.db_manager.transaction() as conn:
            rows = await self._fetch(
                conn,
                "SELECT namespace, name, uid, first_seen_at, last_seen_at FROM deletion_candidates "
                "WHERE resource_kind = ?",
                [resource_kind],
            )
            previous = {(row["namespace"], row["name"]): row for row in rows}
            current = set()
            for candidate in candidates:
                key = (candidate.namespace or "", candidate.name)
                current.add(key)
                row = previous.get(key)
                first_seen = now
                if row is not None and (row["uid"] or "") == (candidate.uid or ""):
                    last_seen = ensure_utc(row["last_seen_at"])
                    if now - last_seen <= max_gap:
                        first_seen = min(ensure_utc(row["first_seen_at"]), now)
                evidence = json.dumps(list(candidate.evidence))
                if row is None:
                    await self._execute(
                        conn,
                        "INSERT INTO deletion_candidates "
                        "(resource_kind, namespace, name, uid, first_seen_at, last_seen_at, evidence) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            resource_kind,
                            key[0],
                            key[1],
                            candidate.uid or "",
                            self._ts(first_seen),
                            self._ts(now),
                            evidence,
                        ],
                    )
                else:
                    await self._execute(
                        conn,
                        "UPDATE deletion_candidates SET uid = ?, first_seen_at = ?, last_seen_at = ?, evidence = ? "
                        "WHERE resource_kind = ? AND namespace = ? AND name = ?",
                        [candidate.uid or "", self._ts(first_seen), self._ts(now), evidence, resource_kind, *key],
                    )
                result[key] = first_seen
            for key in previous:
                if key not in current:
                    await self._execute(
                        conn,
                        "DELETE FROM deletion_candidates WHERE resource_kind = ? AND namespace = ? AND name = ?",
                        [resource_kind, *key],
                    )
        return result

    async def list(self, resource_kind: Optional[str] = None) -> List[dict]:
        """Stored candidates (all kinds, or one), with decoded timestamps and evidence."""
        query = (
            "SELECT resource_kind, namespace, name, uid, first_seen_at, last_seen_at, evidence FROM deletion_candidates"
        )
        params: List[Any] = []
        if resource_kind is not None:
            query += " WHERE resource_kind = ?"
            params.append(resource_kind)
        query += " ORDER BY resource_kind, namespace, name"
        async with self.db_manager.connection_scope() as conn:
            rows = await self._fetch(conn, query, params)
        for row in rows:
            row["first_seen_at"] = ensure_utc(row["first_seen_at"])
            row["last_seen_at"] = ensure_utc(row["last_seen_at"])
            row["evidence"] = json.loads(row["evidence"]) if row.get("evidence") else []
        return rows


def deletion_candidate_repository_for(combined_repo: object) -> Optional[DeletionCandidateRepository]:
    """The candidate repository sharing the database of a SQL combined-metrics repository, if any."""
    from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
    from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

    if isinstance(combined_repo, (PostgresCombinedMetricsRepository, SQLiteCombinedMetricsRepository)):
        return DeletionCandidateRepository(combined_repo.db_manager)
    return None
