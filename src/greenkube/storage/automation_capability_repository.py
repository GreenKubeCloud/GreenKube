# src/greenkube/storage/automation_capability_repository.py
"""Capability state of always-on automation (migration 0071) on SQLite and PostgreSQL.

The controller's ``AutomationReadiness`` reconciler (roadmap F12) replaces the whole
snapshot every minute: one row per global capability and one row per discovered
repository. API replicas only read it (setup checklist F1, repository suggestions
F2). Administrators can additionally allow repositories (F2); those rows are kept
until removed and are merged into provider allowlists by the reconciler.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from greenkube.utils.date_utils import ensure_utc, to_iso_z

_PLACEHOLDER = re.compile(r"\?")

SCOPE_GLOBAL = "global"
SCOPE_REPOSITORY = "repository"


@dataclass(frozen=True)
class CapabilityRow:
    """One persisted capability state."""

    scope: str
    subject: str
    state: str
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


@dataclass(frozen=True)
class AllowedRepository:
    """A repository an administrator allowed from the suggestions (F2)."""

    repo_url: str
    host: str
    repository: str
    created_by: str
    created_at: datetime


class AutomationCapabilityRepository:
    """Reads and replaces the readiness snapshot; stores administrator-allowed repositories."""

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

    async def _fetch(self, conn: Any, query: str, params: Sequence[Any] = ()) -> list[dict]:
        sql = self._sql(query)
        if self._postgres:
            return [dict(row) for row in await conn.fetch(sql, *params)]
        cursor = await conn.execute(sql, list(params))
        rows = await cursor.fetchall()
        names = [column[0] for column in cursor.description or []]
        return [dict(zip(names, row)) for row in rows]

    async def _execute(self, conn: Any, query: str, params: Sequence[Any] = ()) -> None:
        sql = self._sql(query)
        if self._postgres:
            await conn.execute(sql, *params)
        else:
            await conn.execute(sql, list(params))

    # -- readiness snapshot -------------------------------------------------------

    async def replace_snapshot(self, rows: Sequence[CapabilityRow], *, now: datetime | None = None) -> None:
        """Replace every capability row with ``rows`` in one transaction."""
        stamp = self._ts(now or datetime.now(timezone.utc))
        keep = {(row.scope, row.subject) for row in rows}
        async with self.db_manager.transaction() as conn:
            existing = await self._fetch(conn, "SELECT scope, subject FROM automation_capabilities")
            for row in existing:
                if (row["scope"], row["subject"]) not in keep:
                    await self._execute(
                        conn,
                        "DELETE FROM automation_capabilities WHERE scope = ? AND subject = ?",
                        [row["scope"], row["subject"]],
                    )
            for row in rows:
                await self._execute(
                    conn,
                    "INSERT INTO automation_capabilities (scope, subject, state, payload, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (scope, subject) DO UPDATE SET "
                    "state = excluded.state, payload = excluded.payload, updated_at = excluded.updated_at",
                    [row.scope, row.subject, row.state, json.dumps(row.payload, sort_keys=True), stamp],
                )

    async def list_capabilities(self, scope: str | None = None) -> list[CapabilityRow]:
        query = "SELECT scope, subject, state, payload, updated_at FROM automation_capabilities"
        params: list[Any] = []
        if scope is not None:
            query += " WHERE scope = ?"
            params.append(scope)
        query += " ORDER BY scope, subject"
        async with self.db_manager.connection_scope() as conn:
            rows = await self._fetch(conn, query, params)
        return [
            CapabilityRow(
                scope=row["scope"],
                subject=row["subject"],
                state=row["state"],
                payload=_decode(row.get("payload")),
                updated_at=ensure_utc(row["updated_at"]) if row.get("updated_at") else None,
            )
            for row in rows
        ]

    # -- administrator allowlist (F2) ---------------------------------------------

    async def add_allowed_repositories(
        self,
        repositories: Sequence[Mapping[str, str]],
        *,
        actor: str,
        now: datetime | None = None,
    ) -> int:
        """Insert ``{repo_url, host, repository}`` entries; return how many were new."""
        stamp = self._ts(now or datetime.now(timezone.utc))
        added = 0
        async with self.db_manager.transaction() as conn:
            existing = {
                row["repo_url"]
                for row in await self._fetch(conn, "SELECT repo_url FROM automation_repository_allowlist")
            }
            for entry in repositories:
                if entry["repo_url"] in existing:
                    continue
                await self._execute(
                    conn,
                    "INSERT INTO automation_repository_allowlist (repo_url, host, repository, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [entry["repo_url"], entry["host"], entry["repository"], actor, stamp],
                )
                existing.add(entry["repo_url"])
                added += 1
        return added

    async def list_allowed_repositories(self) -> list[AllowedRepository]:
        async with self.db_manager.connection_scope() as conn:
            rows = await self._fetch(
                conn,
                "SELECT repo_url, host, repository, created_by, created_at FROM automation_repository_allowlist "
                "ORDER BY repo_url",
            )
        return [
            AllowedRepository(
                repo_url=row["repo_url"],
                host=row["host"],
                repository=row["repository"],
                created_by=row["created_by"],
                created_at=ensure_utc(row["created_at"]),
            )
            for row in rows
        ]


def _decode(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
