"""PostgreSQL inbox of pushed signals (roadmap G2, G6; migration 0070)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from greenkube.signals.connectors.inbox import InboxBatch, InboxSignal, SignalInboxRepository
from greenkube.signals.model import SourceSignal
from greenkube.storage.inbox_mapper import batch_from_row, inbox_signal_from_row, like_prefix


def _rowcount(status: Any) -> int:
    """Rows affected from an asyncpg command tag (``DELETE 3``)."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


class PostgresSignalInboxRepository(SignalInboxRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def accept(self, batch: InboxBatch, signals: Sequence[SourceSignal]) -> InboxBatch:
        async with self.db_manager.transaction() as conn:
            if signals:
                await conn.executemany(
                    """INSERT INTO source_inbox_signals (source_id, signal_id, batch_id, namespace, received_at, signal)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    ON CONFLICT (source_id, signal_id) DO UPDATE SET
                        batch_id = EXCLUDED.batch_id,
                        namespace = EXCLUDED.namespace,
                        received_at = EXCLUDED.received_at,
                        signal = EXCLUDED.signal""",
                    [
                        (
                            batch.source_id,
                            signal.signal_id,
                            batch.batch_id,
                            signal.target.namespace,
                            batch.received_at,
                            signal.model_dump_json(),
                        )
                        for signal in signals
                    ],
                )
            withdrawn = 0
            if batch.complete:
                status = await conn.execute(
                    "DELETE FROM source_inbox_signals WHERE source_id = $1 AND batch_id <> $2",
                    batch.source_id,
                    batch.batch_id,
                )
                withdrawn = _rowcount(status)
            await conn.execute(
                """INSERT INTO source_inbox_batches (batch_id, source_id, received_at, complete, signal_count,
                withdrawn_count, pushed_by, diagnostics) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)""",
                batch.batch_id,
                batch.source_id,
                batch.received_at,
                batch.complete,
                batch.signal_count,
                withdrawn,
                batch.pushed_by,
                json.dumps([item.model_dump(mode="json") for item in batch.diagnostics]),
            )
        return replace(batch, withdrawn=withdrawn)

    async def _rows(self, query: str, *params) -> List[Mapping]:
        async with self.db_manager.connection_scope() as conn:
            return list(await conn.fetch(query, *params))

    async def count(self, source_id: str) -> int:
        rows = await self._rows("SELECT COUNT(*) AS n FROM source_inbox_signals WHERE source_id = $1", source_id)
        return int(rows[0]["n"]) if rows else 0

    @staticmethod
    def _filter(source_id: Optional[str], source_prefix: Optional[str]) -> tuple[str, list]:
        if source_id is not None:
            return "source_id = $1", [source_id]
        if source_prefix is not None:
            return "source_id LIKE $1 ESCAPE '\\'", [like_prefix(source_prefix)]
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
            params.append(since)
            where += f" AND received_at >= ${len(params)}"
        if namespace is not None:
            params.append(namespace)
            where += f" AND namespace = ${len(params)}"
        rows = await self._rows(
            f"SELECT * FROM source_inbox_signals WHERE {where} ORDER BY source_id, signal_id",  # nosec B608
            *params,
        )
        return [item for item in (inbox_signal_from_row(row) for row in rows) if item is not None]

    async def latest_batches(
        self, *, source_id: Optional[str] = None, source_prefix: Optional[str] = None
    ) -> Dict[str, InboxBatch]:
        where, params = self._filter(source_id, source_prefix)
        rows = await self._rows(
            f"SELECT DISTINCT ON (source_id) * FROM source_inbox_batches WHERE {where} "  # nosec B608
            "ORDER BY source_id, received_at DESC, batch_id",
            *params,
        )
        return {batch.source_id: batch for batch in (batch_from_row(row) for row in rows)}

    async def prune(self, before: datetime) -> int:
        async with self.db_manager.transaction() as conn:
            signals = await conn.execute("DELETE FROM source_inbox_signals WHERE received_at < $1", before)
            batches = await conn.execute("DELETE FROM source_inbox_batches WHERE received_at < $1", before)
        return _rowcount(signals) + _rowcount(batches)
