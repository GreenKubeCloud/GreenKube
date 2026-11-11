"""Inbox of pushed signals (roadmap G2, G6; migration 0070).

Signals that reach GreenKube over HTTP (``POST /api/v1/signals`` batches,
``POST /api/v1/sources/krr/reports``) are stored here, then replayed into each
scan by their built-in source (``push``, ``krr``). They go through the source
runner like every other signal (validation, ``source_runs``/``source_signals``,
then consensus), so a push never bypasses scans, consensus or risk review.

* ``source_inbox_batches``: one row per accepted batch (source, received time,
  completeness, counts, who pushed it, diagnostics).
* ``source_inbox_signals``: the current signals of each inbox source, one row
  per ``(source_id, signal_id)``. Pushing the same signal again only refreshes
  it (idempotent on ``signal_id``). A ``complete`` batch withdraws the
  source's signals it does not contain.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from greenkube.signals.model import SourceSignal
from greenkube.signals.sdk import Diagnostic

logger = logging.getLogger(__name__)

#: Signals one inbox source may hold (incomplete batches accumulate).
MAX_INBOX_SIGNALS_PER_SOURCE = 50_000


@dataclass(frozen=True)
class InboxBatch:
    """One accepted push."""

    batch_id: str
    source_id: str
    received_at: datetime
    complete: bool
    signal_count: int = 0
    #: Audit actor of the caller (``apikey:ci``, ``user:alice``).
    pushed_by: Optional[str] = None
    diagnostics: List[Diagnostic] = field(default_factory=list)
    #: Signals the batch withdrew (``complete`` batches only); set by the repository.
    withdrawn: int = 0


@dataclass(frozen=True)
class InboxSignal:
    """A stored pushed signal and when it was last pushed."""

    signal: SourceSignal
    batch_id: str
    received_at: datetime


class SignalInboxRepository(ABC):
    """Stores pushed batches and their current signals."""

    @abstractmethod
    async def accept(self, batch: InboxBatch, signals: Sequence[SourceSignal]) -> InboxBatch:
        """Record ``batch`` and upsert its signals in one transaction; returns it with ``withdrawn`` set."""

    @abstractmethod
    async def count(self, source_id: str) -> int:
        """Signals currently held for ``source_id``."""

    @abstractmethod
    async def current(
        self,
        *,
        source_id: Optional[str] = None,
        source_prefix: Optional[str] = None,
        since: Optional[datetime] = None,
        namespace: Optional[str] = None,
    ) -> List[InboxSignal]:
        """Current signals of one source (or of every source under a prefix), received at or after ``since``."""

    @abstractmethod
    async def latest_batches(
        self, *, source_id: Optional[str] = None, source_prefix: Optional[str] = None
    ) -> Dict[str, InboxBatch]:
        """Latest batch of each matching source."""

    @abstractmethod
    async def prune(self, before: datetime) -> int:
        """Delete batches and signals received before ``before``; returns rows deleted."""


def create_signal_inbox_repository(db_manager: Any = None, db_type: Optional[str] = None) -> SignalInboxRepository:
    """The inbox for the configured backend."""
    if db_manager is None or db_type is None:
        from greenkube.core.config import get_config
        from greenkube.core.db import get_db_manager

        db_manager = db_manager if db_manager is not None else get_db_manager()
        db_type = db_type or get_config().DB_TYPE
    if db_type == "postgres":
        from greenkube.storage.postgres.signal_inbox_repository import PostgresSignalInboxRepository

        return PostgresSignalInboxRepository(db_manager)
    if db_type == "sqlite":
        from greenkube.storage.sqlite.signal_inbox_repository import SQLiteSignalInboxRepository

        return SQLiteSignalInboxRepository(db_manager)
    raise NotImplementedError(f"SignalInboxRepository for DB_TYPE '{db_type}' not implemented.")
