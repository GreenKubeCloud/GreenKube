"""Repository contract for source runs and source signals (roadmap E5).

* ``source_runs``: one row per source per scan (status, health, counts,
  consecutive failures, error code, diagnostics).
* ``source_signals``: one row per distinct signal (``source_id``,
  ``signal_id``). A signal re-emitted by later runs is not duplicated: its
  ``last_seen_at``/``last_run_id`` move forward and its payload is refreshed.
  Because ``signal_id`` hashes the proposal, a changed proposal is a new row,
  so the history of a source's proposals survives for the retention period.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from greenkube.signals.model import SourceSignal
from greenkube.signals.runtime import SourceRunRecord

#: Upper bound for list endpoints.
MAX_LIST_LIMIT = 500


def bounded_limit(limit: int, maximum: int = MAX_LIST_LIMIT) -> int:
    return max(1, min(int(limit), maximum))


@dataclass(frozen=True)
class StoredSignal:
    """A persisted signal with its sighting window."""

    signal: SourceSignal
    first_seen_at: datetime
    last_seen_at: datetime
    last_run_id: Optional[int]
    recommendation_fingerprint: Optional[str]


@dataclass(frozen=True)
class SourceRunSummary:
    """Latest run of a source, plus when it last succeeded."""

    latest: SourceRunRecord
    last_success_at: Optional[datetime]


class SignalRepository(ABC):
    """Persists source runs and the signals they produced."""

    @abstractmethod
    async def record_run(
        self,
        run: SourceRunRecord,
        signals: Sequence[SourceSignal] = (),
    ) -> SourceRunRecord:
        """Insert ``run`` and upsert its signals in one transaction; returns the stored run."""

    @abstractmethod
    async def latest_runs(self) -> Dict[str, SourceRunSummary]:
        """Latest run and last success time of every source that ever ran."""

    @abstractmethod
    async def list_runs(self, source_id: str, *, limit: int = 20) -> List[SourceRunRecord]:
        """A source's runs, newest first."""

    @abstractmethod
    async def list_signals(
        self,
        source_id: str,
        *,
        limit: int = 100,
        target_key: Optional[str] = None,
    ) -> List[StoredSignal]:
        """A source's signals, most recently seen first."""

    @abstractmethod
    async def prune(self, before: datetime) -> int:
        """Delete runs started and signals last seen before ``before``; returns rows deleted."""


def create_signal_repository(db_manager: Any = None, db_type: Optional[str] = None) -> SignalRepository:
    """Build the repository for the configured backend."""
    if db_manager is None or db_type is None:
        from greenkube.core.config import get_config
        from greenkube.core.db import get_db_manager

        db_manager = db_manager if db_manager is not None else get_db_manager()
        db_type = db_type or get_config().DB_TYPE
    if db_type == "postgres":
        from greenkube.storage.postgres.signal_repository import PostgresSignalRepository

        return PostgresSignalRepository(db_manager)
    if db_type == "sqlite":
        from greenkube.storage.sqlite.signal_repository import SQLiteSignalRepository

        return SQLiteSignalRepository(db_manager)
    raise NotImplementedError(f"SignalRepository for DB_TYPE '{db_type}' not implemented.")
