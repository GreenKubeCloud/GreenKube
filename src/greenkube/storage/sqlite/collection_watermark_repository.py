"""SQLite collection watermark repository."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from greenkube.storage.base_collection_watermark_repository import CollectionWatermark, CollectionWatermarkRepository
from greenkube.utils.time import ensure_utc

# Fixed-width UTC format so that lexicographic MAX() equals chronological MAX().
_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _encode(value: datetime) -> str:
    return ensure_utc(value).strftime(_FORMAT)


def _decode(value: str) -> datetime:
    return ensure_utc(value)


def _row(row) -> CollectionWatermark:
    return CollectionWatermark(
        cluster_uid=row[0],
        source=row[1],
        last_complete_end=_decode(row[2]),
        updated_at=_decode(row[3]),
    )


class SQLiteCollectionWatermarkRepository(CollectionWatermarkRepository):
    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def get(self, cluster_uid: str, source: str) -> CollectionWatermark | None:
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(
                """SELECT cluster_uid, source, last_complete_end, updated_at
                FROM collection_watermarks WHERE cluster_uid = ? AND source = ?""",
                (cluster_uid, source),
            ) as cursor:
                row = await cursor.fetchone()
        return _row(row) if row else None

    async def advance(
        self, cluster_uid: str, source: str, last_complete_end: datetime, *, connection: Any = None
    ) -> CollectionWatermark:
        """See :meth:`CollectionWatermarkRepository.advance`."""
        now = _encode(datetime.now(timezone.utc))
        query = """INSERT INTO collection_watermarks (cluster_uid, source, last_complete_end, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cluster_uid, source) DO UPDATE SET
                    last_complete_end = MAX(collection_watermarks.last_complete_end, excluded.last_complete_end),
                    updated_at = excluded.updated_at"""
        params = (cluster_uid, source, _encode(last_complete_end), now)
        if connection is not None:
            await connection.execute(query, params)
        else:
            async with self.db_manager.transaction() as conn:
                await conn.execute(query, params)
        watermark = await self.get(cluster_uid, source)
        if watermark is None:  # pragma: no cover - the upsert above guarantees a row
            raise RuntimeError("Collection watermark was not persisted.")
        return watermark

    async def list_all(self, cluster_uid: str | None = None) -> list[CollectionWatermark]:
        query = "SELECT cluster_uid, source, last_complete_end, updated_at FROM collection_watermarks"
        params: tuple[str, ...] = ()
        if cluster_uid is not None:
            query += " WHERE cluster_uid = ?"
            params = (cluster_uid,)
        query += " ORDER BY cluster_uid, source"
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        return [_row(row) for row in rows]
