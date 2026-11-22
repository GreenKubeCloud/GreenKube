# src/greenkube/storage/resource_binding_repository.py
"""Persisted resource bindings (migration 0087) on SQLite and PostgreSQL (roadmap H0d, H6).

Discovery runs in the controller and plans are created by the API, so the
``ResourceBindingIndex`` built from the Argo CD and Flux inventories is persisted:
each discovery refresh replaces the rows of its cluster with the bindings of the kinds
registered action kinds target (``ActionKindSpec.resource_kinds``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

from greenkube.gitops.contracts import ResourceTarget
from greenkube.gitops.discovery.inventory import GroupVersionKind, ResourceBinding
from greenkube.utils.date_utils import ensure_utc, to_iso_z

_PLACEHOLDER = re.compile(r"\?")


@dataclass(frozen=True)
class StoredResourceBinding:
    """One persisted binding and when discovery observed it."""

    binding: ResourceBinding
    observed_at: datetime


class ResourceBindingRepository:
    """Replaces and reads ``resource_bindings`` rows."""

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

    async def replace_cluster(self, cluster: str, bindings: Iterable[ResourceBinding], *, observed_at: datetime) -> int:
        """Replace every binding of ``cluster`` with ``bindings`` in one transaction."""
        rows = []
        for binding in bindings:
            gvk = GroupVersionKind.from_api_version(binding.ref.api_version, binding.ref.kind)
            payload = json.dumps(binding.to_payload(), sort_keys=True, separators=(",", ":"), default=str)
            rows.append(
                [
                    cluster,
                    gvk.group,
                    gvk.kind,
                    binding.ref.namespace,
                    binding.ref.name,
                    binding.status,
                    payload,
                    self._ts(observed_at),
                ]
            )
        payload_param = "?::jsonb" if self._postgres else "?"
        insert = self._sql(
            "INSERT INTO resource_bindings (cluster, api_group, kind, namespace, name, status, payload, observed_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, {payload_param}, ?) "
            "ON CONFLICT (cluster, api_group, kind, namespace, name) DO UPDATE SET status = excluded.status, "
            "payload = excluded.payload, observed_at = excluded.observed_at"
        )
        delete = self._sql("DELETE FROM resource_bindings WHERE cluster = ?")
        async with self.db_manager.transaction() as conn:
            if self._postgres:
                await conn.execute(delete, cluster)
                for row in rows:
                    await conn.execute(insert, *row)
            else:
                await conn.execute(delete, [cluster])
                for row in rows:
                    await conn.execute(insert, row)
        return len(rows)

    async def get(self, cluster: str, ref: ResourceTarget) -> Optional[StoredResourceBinding]:
        """The binding of ``ref`` (version-insensitive), or ``None`` when no controller lists it."""
        gvk = GroupVersionKind.from_api_version(ref.api_version, ref.kind)
        rows = await self._fetch(
            "SELECT payload, observed_at FROM resource_bindings "
            "WHERE cluster = ? AND api_group = ? AND kind = ? AND namespace = ? AND name = ?",
            [cluster, gvk.group, gvk.kind, ref.namespace, ref.name],
        )
        if not rows:
            return None
        binding = ResourceBinding.from_payload(_json(rows[0]["payload"]))
        # The stored ref may carry another API version of the same object: answer with the asked one.
        return StoredResourceBinding(binding=replace(binding, ref=ref), observed_at=_time(rows[0]["observed_at"]))

    async def list_cluster(self, cluster: str) -> List[StoredResourceBinding]:
        rows = await self._fetch(
            "SELECT payload, observed_at FROM resource_bindings WHERE cluster = ? "
            "ORDER BY api_group, kind, namespace, name",
            [cluster],
        )
        return [
            StoredResourceBinding(ResourceBinding.from_payload(_json(row["payload"])), _time(row["observed_at"]))
            for row in rows
        ]

    async def _fetch(self, query: str, params: Sequence[Any]) -> List[dict]:
        sql = self._sql(query)
        async with self.db_manager.connection_scope() as conn:
            if self._postgres:
                return [dict(row) for row in await conn.fetch(sql, *params)]
            cursor = await conn.execute(sql, list(params))
            rows = await cursor.fetchall()
            names = [column[0] for column in cursor.description or []]
            return [dict(zip(names, row)) for row in rows]


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return datetime.fromtimestamp(0, tz=timezone.utc)


def get_resource_binding_repository(db_manager: Optional[Any] = None) -> ResourceBindingRepository:
    if db_manager is None:
        from greenkube.core.db import get_db_manager

        db_manager = get_db_manager()
    return ResourceBindingRepository(db_manager)
