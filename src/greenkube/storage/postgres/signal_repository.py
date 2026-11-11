"""PostgreSQL source runs and source signals (roadmap E5)."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence

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
    diagnostics_json,
    run_from_row,
    signal_json,
    stored_signal_from_row,
)


def _rowcount(status: str) -> int:
    """Rows affected from an asyncpg command tag (``DELETE 3``)."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


class PostgresSignalRepository(SignalRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def record_run(
        self,
        run: SourceRunRecord,
        signals: Sequence[SourceSignal] = (),
    ) -> SourceRunRecord:
        placeholders = ", ".join(
            f"${index}::jsonb" if column == "diagnostics" else f"${index}"
            for index, column in enumerate(RUN_COLUMNS, start=1)
        )
        values = (
            run.optimization_run_id,
            run.source_id,
            run.source_version,
            run.namespace,
            run.status.value,
            run.complete,
            run.health_status.value if run.health_status is not None else None,
            run.health_reason,
            run.started_at,
            run.finished_at,
            run.duration_seconds,
            run.signal_count,
            run.recommendation_count,
            run.consecutive_failures,
            run.last_collected_at,
            run.error_code,
            run.error,
            diagnostics_json(run),
        )
        async with self.db_manager.transaction() as conn:
            run_id = await conn.fetchval(
                f"INSERT INTO source_runs ({', '.join(RUN_COLUMNS)}) VALUES ({placeholders}) RETURNING id",
                *values,
            )
            if signals:
                await conn.executemany(
                    """INSERT INTO source_signals (source_id, signal_id, target_key, capability, kind, namespace,
                    recommendation_fingerprint, first_seen_at, last_seen_at, last_run_id, signal)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, $9, $10::jsonb)
                    ON CONFLICT (source_id, signal_id) DO UPDATE SET
                        last_seen_at = EXCLUDED.last_seen_at,
                        last_run_id = EXCLUDED.last_run_id,
                        recommendation_fingerprint = COALESCE(
                            EXCLUDED.recommendation_fingerprint, source_signals.recommendation_fingerprint
                        ),
                        signal = EXCLUDED.signal""",
                    [
                        (
                            signal.source_id,
                            signal.signal_id,
                            signal.target_key,
                            signal.capability.value,
                            signal.kind,
                            signal.target.namespace,
                            signal.provenance.legacy_fingerprint,
                            run.finished_at,
                            run_id,
                            signal_json(signal),
                        )
                        for signal in signals
                    ],
                )
        return run.model_copy(update={"id": run_id})

    async def _rows(self, query: str, *params) -> List[Mapping]:
        async with self.db_manager.connection_scope() as conn:
            return list(await conn.fetch(query, *params))

    async def latest_runs(self) -> Dict[str, SourceRunSummary]:
        latest = await self._rows("SELECT DISTINCT ON (source_id) * FROM source_runs ORDER BY source_id, id DESC")
        successes = await self._rows(
            "SELECT source_id, MAX(finished_at) AS last_success_at FROM source_runs "
            "WHERE status = 'succeeded' GROUP BY source_id"
        )
        success_by_source = {row["source_id"]: row["last_success_at"] for row in successes}
        return {
            row["source_id"]: SourceRunSummary(
                latest=run_from_row(row), last_success_at=success_by_source.get(row["source_id"])
            )
            for row in latest
        }

    async def list_runs(self, source_id: str, *, limit: int = 20) -> List[SourceRunRecord]:
        rows = await self._rows(
            "SELECT * FROM source_runs WHERE source_id = $1 ORDER BY id DESC LIMIT $2",
            source_id,
            bounded_limit(limit),
        )
        return [run_from_row(row) for row in rows]

    async def list_signals(
        self,
        source_id: str,
        *,
        limit: int = 100,
        target_key: Optional[str] = None,
    ) -> List[StoredSignal]:
        if target_key is None:
            rows = await self._rows(
                "SELECT * FROM source_signals WHERE source_id = $1 ORDER BY last_seen_at DESC, signal_id LIMIT $2",
                source_id,
                bounded_limit(limit),
            )
        else:
            rows = await self._rows(
                "SELECT * FROM source_signals WHERE source_id = $1 AND target_key = $2 "
                "ORDER BY last_seen_at DESC, signal_id LIMIT $3",
                source_id,
                target_key,
                bounded_limit(limit),
            )
        return [stored for stored in (stored_signal_from_row(row) for row in rows) if stored is not None]

    async def prune(self, before: datetime) -> int:
        async with self.db_manager.transaction() as conn:
            signals = await conn.execute("DELETE FROM source_signals WHERE last_seen_at < $1", before)
            runs = await conn.execute("DELETE FROM source_runs WHERE started_at < $1", before)
        return _rowcount(signals) + _rowcount(runs)
