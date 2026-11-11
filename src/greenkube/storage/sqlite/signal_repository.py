"""SQLite source runs and source signals (roadmap E5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import aiosqlite

from greenkube.signals.model import SourceSignal
from greenkube.signals.runtime import SourceRunRecord
from greenkube.storage.base_signal_repository import (
    SignalRepository,
    SourceRunSummary,
    StoredSignal,
    bounded_limit,
)
from greenkube.storage.signal_mapper import (
    RUN_COLUMNS,
    aware,
    diagnostics_json,
    run_from_row,
    signal_json,
    stored_signal_from_row,
)


def _ts(value: Optional[datetime]) -> Optional[str]:
    """Fixed-width UTC timestamp so string comparison matches time order."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SQLiteSignalRepository(SignalRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def record_run(
        self,
        run: SourceRunRecord,
        signals: Sequence[SourceSignal] = (),
    ) -> SourceRunRecord:
        values = (
            run.optimization_run_id,
            run.source_id,
            run.source_version,
            run.namespace,
            run.status.value,
            1 if run.complete else 0,
            run.health_status.value if run.health_status is not None else None,
            run.health_reason,
            _ts(run.started_at),
            _ts(run.finished_at),
            run.duration_seconds,
            run.signal_count,
            run.recommendation_count,
            run.consecutive_failures,
            _ts(run.last_collected_at),
            run.error_code,
            run.error,
            diagnostics_json(run),
        )
        seen_at = _ts(run.finished_at)
        async with self.db_manager.transaction() as conn:
            cursor = await conn.execute(
                f"INSERT INTO source_runs ({', '.join(RUN_COLUMNS)}) VALUES ({', '.join('?' for _ in RUN_COLUMNS)})",
                values,
            )
            run_id = cursor.lastrowid
            if signals:
                await conn.executemany(
                    """INSERT INTO source_signals (source_id, signal_id, target_key, capability, kind, namespace,
                    recommendation_fingerprint, first_seen_at, last_seen_at, last_run_id, signal)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id, signal_id) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        last_run_id = excluded.last_run_id,
                        recommendation_fingerprint = COALESCE(
                            excluded.recommendation_fingerprint, source_signals.recommendation_fingerprint
                        ),
                        signal = excluded.signal""",
                    [
                        (
                            signal.source_id,
                            signal.signal_id,
                            signal.target_key,
                            signal.capability.value,
                            signal.kind,
                            signal.target.namespace,
                            signal.provenance.legacy_fingerprint,
                            seen_at,
                            seen_at,
                            run_id,
                            signal_json(signal),
                        )
                        for signal in signals
                    ],
                )
        return run.model_copy(update={"id": run_id})

    async def _rows(self, query: str, params: tuple) -> List[aiosqlite.Row]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            return list(await cursor.fetchall())

    async def latest_runs(self) -> Dict[str, SourceRunSummary]:
        latest = await self._rows(
            # One aggregate pass instead of a correlated MAX(id) per row, which
            # grew quadratically with run history (every scan reads this).
            """SELECT r.* FROM source_runs r
            JOIN (SELECT source_id, MAX(id) AS id FROM source_runs GROUP BY source_id) latest
              ON latest.id = r.id""",
            (),
        )
        successes = await self._rows(
            "SELECT source_id, MAX(finished_at) AS last_success_at FROM source_runs "
            "WHERE status = 'succeeded' GROUP BY source_id",
            (),
        )
        success_by_source = {row["source_id"]: aware(row["last_success_at"]) for row in successes}
        return {
            row["source_id"]: SourceRunSummary(
                latest=run_from_row(row), last_success_at=success_by_source.get(row["source_id"])
            )
            for row in latest
        }

    async def list_runs(self, source_id: str, *, limit: int = 20) -> List[SourceRunRecord]:
        rows = await self._rows(
            "SELECT * FROM source_runs WHERE source_id = ? ORDER BY id DESC LIMIT ?",
            (source_id, bounded_limit(limit)),
        )
        return [run_from_row(row) for row in rows]

    async def list_signals(
        self,
        source_id: str,
        *,
        limit: int = 100,
        target_key: Optional[str] = None,
    ) -> List[StoredSignal]:
        query = "SELECT * FROM source_signals WHERE source_id = ?"
        params: tuple = (source_id,)
        if target_key is not None:
            query += " AND target_key = ?"
            params += (target_key,)
        query += " ORDER BY last_seen_at DESC, signal_id LIMIT ?"
        rows = await self._rows(query, params + (bounded_limit(limit),))
        return [stored for stored in (stored_signal_from_row(row) for row in rows) if stored is not None]

    async def prune(self, before: datetime) -> int:
        cutoff = _ts(before)
        async with self.db_manager.transaction() as conn:
            signals = await conn.execute("DELETE FROM source_signals WHERE last_seen_at < ?", (cutoff,))
            runs = await conn.execute("DELETE FROM source_runs WHERE started_at < ?", (cutoff,))
            return max(signals.rowcount, 0) + max(runs.rowcount, 0)
