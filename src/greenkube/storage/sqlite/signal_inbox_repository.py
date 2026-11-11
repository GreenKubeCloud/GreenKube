"""SQLite inbox of pushed signals (roadmap G2, G6; migration 0070)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import aiosqlite

from greenkube.signals.connectors.inbox import InboxBatch, InboxSignal, SignalInboxRepository
from greenkube.signals.model import SourceSignal
from greenkube.storage.inbox_mapper import batch_from_row, inbox_signal_from_row, like_prefix


def _ts(value: Optional[datetime]) -> Optional[str]:
    """Fixed-width UTC timestamp so string comparison matches time order."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SQLiteSignalInboxRepository(SignalInboxRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def accept(self, batch: InboxBatch, signals: Sequence[SourceSignal]) -> InboxBatch:
        received = _ts(batch.received_at)
        async with self.db_manager.transaction() as conn:
            if signals:
                await conn.executemany(
                    """INSERT INTO source_inbox_signals (source_id, signal_id, batch_id, namespace, received_at, signal)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id, signal_id) DO UPDATE SET
                        batch_id = excluded.batch_id,
                        namespace = excluded.namespace,
                        received_at = excluded.received_at,
                        signal = excluded.signal""",
                    [
                        (
                            batch.source_id,
                            signal.signal_id,
                            batch.batch_id,
                            signal.target.namespace,
                            received,
                            signal.model_dump_json(),
                        )
                        for signal in signals
                    ],
                )
            withdrawn = 0
            if batch.complete:
                cursor = await conn.execute(
                    "DELETE FROM source_inbox_signals WHERE source_id = ? AND batch_id <> ?",
                    (batch.source_id, batch.batch_id),
                )
                withdrawn = max(cursor.rowcount, 0)
            await conn.execute(
                """INSERT INTO source_inbox_batches (batch_id, source_id, received_at, complete, signal_count,
                withdrawn_count, pushed_by, diagnostics) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch.batch_id,
                    batch.source_id,
                    received,
                    1 if batch.complete else 0,
                    batch.signal_count,
                    withdrawn,
                    batch.pushed_by,
                    json.dumps([item.model_dump(mode="json") for item in batch.diagnostics]),
                ),
            )
        return replace(batch, withdrawn=withdrawn)

    async def _rows(self, query: str, params: tuple) -> List[aiosqlite.Row]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            return list(await cursor.fetchall())

    async def count(self, source_id: str) -> int:
        rows = await self._rows("SELECT COUNT(*) AS n FROM source_inbox_signals WHERE source_id = ?", (source_id,))
        return int(rows[0]["n"]) if rows else 0

    def _filter(self, source_id: Optional[str], source_prefix: Optional[str]) -> tuple[str, tuple]:
        if source_id is not None:
            return "source_id = ?", (source_id,)
        if source_prefix is not None:
            return "source_id LIKE ? ESCAPE '\\'", (like_prefix(source_prefix),)
        raise ValueError("source_id or source_prefix is required")

    async def current(
        self,
        *,
        source_id: Optional[str] = None,
        source_prefix: Optional[str] = None,
        since: Optional[datetime] = None,
        namespace: Optional[str] = None,
    ) -> List[InboxSignal]:
        where, params = self._filter(source_id, source_prefix)
        if since is not None:
            where += " AND received_at >= ?"
            params += (_ts(since),)
        if namespace is not None:
            where += " AND namespace = ?"
            params += (namespace,)
        rows = await self._rows(
            f"SELECT * FROM source_inbox_signals WHERE {where} ORDER BY source_id, signal_id",  # nosec B608
            params,
        )
        return [item for item in (inbox_signal_from_row(row) for row in rows) if item is not None]

    async def latest_batches(
        self, *, source_id: Optional[str] = None, source_prefix: Optional[str] = None
    ) -> Dict[str, InboxBatch]:
        where, params = self._filter(source_id, source_prefix)
        rows = await self._rows(
            f"""SELECT b.* FROM source_inbox_batches b
            JOIN (SELECT source_id, MAX(received_at) AS received_at FROM source_inbox_batches
                  WHERE {where} GROUP BY source_id) latest
              ON latest.source_id = b.source_id AND latest.received_at = b.received_at""",  # nosec B608
            params,
        )
        result: Dict[str, InboxBatch] = {}
        for row in rows:
            batch = batch_from_row(row)
            result[batch.source_id] = batch
        return result

    async def prune(self, before: datetime) -> int:
        cutoff = _ts(before)
        async with self.db_manager.transaction() as conn:
            signals: Any = await conn.execute("DELETE FROM source_inbox_signals WHERE received_at < ?", (cutoff,))
            batches: Any = await conn.execute("DELETE FROM source_inbox_batches WHERE received_at < ?", (cutoff,))
            return max(signals.rowcount, 0) + max(batches.rowcount, 0)
