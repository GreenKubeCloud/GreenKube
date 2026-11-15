"""Installation identity and adoption of legacy collection keys (COL-02, STO-09)."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from greenkube.utils.time import ensure_utc

#: A kube-system namespace UID (RFC 4122 text form). Other watermark keys are legacy names.
_UID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SQLITE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def is_cluster_uid(key: str) -> bool:
    """True when ``key`` is a kube-system namespace UID, not a legacy cluster or node name."""
    return bool(_UID_RE.match(key or ""))


@dataclass(frozen=True)
class InstallationIdentity:
    """The cluster this database schema belongs to."""

    cluster_uid: str
    cluster_name: str | None
    source: str


@dataclass(frozen=True)
class AdoptedKeys:
    """Legacy watermark keys merged into the installation identity."""

    keys: tuple[str, ...]
    watermark: datetime | None


class InstallationRepository(ABC):
    """Stores the single installation row and re-keys legacy collection state."""

    @abstractmethod
    async def get(self) -> InstallationIdentity | None:
        """The installation identity, or ``None`` before the first controller start."""

    @abstractmethod
    async def claim(self, identity: InstallationIdentity) -> InstallationIdentity:
        """Store ``identity`` unless one exists; return the stored identity."""

    @abstractmethod
    async def replace(self, identity: InstallationIdentity) -> InstallationIdentity:
        """Overwrite the stored identity (explicit cluster UID change)."""

    @abstractmethod
    async def adopt_legacy_keys(self, cluster_uid: str, source: str) -> AdoptedKeys:
        """Merge watermark rows and window records keyed by a name into ``cluster_uid``.

        Keys that are not a kube-system UID (an old ``CLUSTER_NAME``, a node
        name, ``default`` or ``name:<CLUSTER_NAME>``) were written by this
        installation before its identity existed. The watermark becomes the
        maximum of theirs and the current one; window records are moved
        (a record already stored under ``cluster_uid`` wins); legacy rows are
        deleted.
        """


class SQLiteInstallationRepository(InstallationRepository):
    """Installation identity in SQLite."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def get(self) -> InstallationIdentity | None:
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute("SELECT cluster_uid, cluster_name, source FROM installation WHERE id = 1") as cur:
                row = await cur.fetchone()
        return InstallationIdentity(row[0], row[1], row[2]) if row else None

    async def claim(self, identity: InstallationIdentity) -> InstallationIdentity:
        now = datetime.now(timezone.utc).strftime(_SQLITE_FORMAT)
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """INSERT INTO installation (id, cluster_uid, cluster_name, source, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING""",
                (identity.cluster_uid, identity.cluster_name, identity.source, now, now),
            )
            await conn.commit()
        stored = await self.get()
        if stored is None:  # pragma: no cover - the insert above guarantees a row
            raise RuntimeError("Installation identity was not persisted.")
        return stored

    async def replace(self, identity: InstallationIdentity) -> InstallationIdentity:
        now = datetime.now(timezone.utc).strftime(_SQLITE_FORMAT)
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """INSERT INTO installation (id, cluster_uid, cluster_name, source, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                    cluster_uid = excluded.cluster_uid, cluster_name = excluded.cluster_name,
                    source = excluded.source, updated_at = excluded.updated_at""",
                (identity.cluster_uid, identity.cluster_name, identity.source, now, now),
            )
            await conn.commit()
        return identity

    async def adopt_legacy_keys(self, cluster_uid: str, source: str) -> AdoptedKeys:
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                "SELECT cluster_uid, last_complete_end FROM collection_watermarks WHERE source = ?", (source,)
            ) as cur:
                rows = await cur.fetchall()
            async with conn.execute(
                "SELECT DISTINCT cluster_uid FROM collection_windows WHERE source = ?", (source,)
            ) as cur:
                window_keys = {row[0] for row in await cur.fetchall()}
            legacy = sorted(
                {row[0] for row in rows if row[0] != cluster_uid and not is_cluster_uid(row[0])}
                | {key for key in window_keys if key != cluster_uid and not is_cluster_uid(key)}
            )
            if not legacy:
                return AdoptedKeys((), None)
            ends = [row[1] for row in rows if row[0] in legacy or row[0] == cluster_uid]
            watermark = max(ends) if ends else None
            marks = ",".join("?" for _ in legacy)
            if watermark is not None:
                await conn.execute(
                    """INSERT INTO collection_watermarks (cluster_uid, source, last_complete_end, updated_at)
                    VALUES (?, ?, ?, ?) ON CONFLICT(cluster_uid, source) DO UPDATE SET
                        last_complete_end = MAX(collection_watermarks.last_complete_end, excluded.last_complete_end),
                        updated_at = excluded.updated_at""",
                    (cluster_uid, source, watermark, datetime.now(timezone.utc).strftime(_SQLITE_FORMAT)),
                )
            await conn.execute(
                f"""UPDATE OR IGNORE collection_windows SET cluster_uid = ?
                WHERE source = ? AND cluster_uid IN ({marks})""",
                (cluster_uid, source, *legacy),
            )
            await conn.execute(
                f"DELETE FROM collection_windows WHERE source = ? AND cluster_uid IN ({marks})", (source, *legacy)
            )
            await conn.execute(
                f"DELETE FROM collection_watermarks WHERE source = ? AND cluster_uid IN ({marks})", (source, *legacy)
            )
            await conn.commit()
        return AdoptedKeys(tuple(legacy), ensure_utc(watermark) if watermark else None)


class PostgresInstallationRepository(InstallationRepository):
    """Installation identity in PostgreSQL."""

    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def get(self) -> InstallationIdentity | None:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT cluster_uid, cluster_name, source FROM installation WHERE id = 1")
        return InstallationIdentity(row["cluster_uid"], row["cluster_name"], row["source"]) if row else None

    async def claim(self, identity: InstallationIdentity) -> InstallationIdentity:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """INSERT INTO installation (id, cluster_uid, cluster_name, source)
                VALUES (1, $1, $2, $3) ON CONFLICT (id) DO NOTHING""",
                identity.cluster_uid,
                identity.cluster_name,
                identity.source,
            )
        stored = await self.get()
        if stored is None:  # pragma: no cover - the insert above guarantees a row
            raise RuntimeError("Installation identity was not persisted.")
        return stored

    async def replace(self, identity: InstallationIdentity) -> InstallationIdentity:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """INSERT INTO installation (id, cluster_uid, cluster_name, source)
                VALUES (1, $1, $2, $3) ON CONFLICT (id) DO UPDATE SET
                    cluster_uid = EXCLUDED.cluster_uid, cluster_name = EXCLUDED.cluster_name,
                    source = EXCLUDED.source, updated_at = NOW()""",
                identity.cluster_uid,
                identity.cluster_name,
                identity.source,
            )
        return identity

    async def adopt_legacy_keys(self, cluster_uid: str, source: str) -> AdoptedKeys:
        async with self.db_manager.connection_scope() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT cluster_uid, last_complete_end FROM collection_watermarks WHERE source = $1", source
                )
                window_keys = {
                    row["cluster_uid"]
                    for row in await conn.fetch(
                        "SELECT DISTINCT cluster_uid FROM collection_windows WHERE source = $1", source
                    )
                }
                legacy = sorted(
                    {
                        r["cluster_uid"]
                        for r in rows
                        if r["cluster_uid"] != cluster_uid and not is_cluster_uid(r["cluster_uid"])
                    }
                    | {key for key in window_keys if key != cluster_uid and not is_cluster_uid(key)}
                )
                if not legacy:
                    return AdoptedKeys((), None)
                ends = [
                    r["last_complete_end"]
                    for r in rows
                    if r["cluster_uid"] in legacy or r["cluster_uid"] == cluster_uid
                ]
                watermark = max(ends) if ends else None
                if watermark is not None:
                    await conn.execute(
                        """INSERT INTO collection_watermarks (cluster_uid, source, last_complete_end, updated_at)
                        VALUES ($1, $2, $3, NOW()) ON CONFLICT (cluster_uid, source) DO UPDATE SET
                            last_complete_end = GREATEST(collection_watermarks.last_complete_end,
                                                         EXCLUDED.last_complete_end),
                            updated_at = NOW()""",
                        cluster_uid,
                        source,
                        watermark,
                    )
                await conn.execute(
                    """UPDATE collection_windows AS w SET cluster_uid = $1
                    WHERE w.source = $2 AND w.cluster_uid = ANY($3::text[])
                      AND NOT EXISTS (
                          SELECT 1 FROM collection_windows AS t
                          WHERE t.cluster_uid = $1 AND t.source = w.source AND t.window_start = w.window_start
                      )
                      AND w.ctid = (
                          SELECT ctid FROM collection_windows AS d
                          WHERE d.source = w.source AND d.window_start = w.window_start
                            AND d.cluster_uid = ANY($3::text[])
                          ORDER BY d.collected_at DESC LIMIT 1
                      )""",
                    cluster_uid,
                    source,
                    legacy,
                )
                await conn.execute(
                    "DELETE FROM collection_windows WHERE source = $1 AND cluster_uid = ANY($2::text[])", source, legacy
                )
                await conn.execute(
                    "DELETE FROM collection_watermarks WHERE source = $1 AND cluster_uid = ANY($2::text[])",
                    source,
                    legacy,
                )
        return AdoptedKeys(tuple(legacy), ensure_utc(watermark) if watermark else None)


def create_installation_repository(db_manager: Any, db_type: str) -> InstallationRepository:
    """Build the installation repository matching ``DB_TYPE``."""
    if db_type == "postgres":
        return PostgresInstallationRepository(db_manager)
    if db_type == "sqlite":
        return SQLiteInstallationRepository(db_manager)
    raise NotImplementedError(f"InstallationRepository for DB_TYPE '{db_type}' not implemented.")
