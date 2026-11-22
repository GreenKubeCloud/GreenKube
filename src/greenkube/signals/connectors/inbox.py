"""Inbox of pushed signals (roadmap G2, G6; migration 0070).

Signals that reach GreenKube over HTTP (``POST /api/v1/signals`` batches,
``POST /api/v1/sources/krr/reports``) are stored here, then read into each
scan by their built-in source (``push``, ``krr``). They go through the source
runner like every other signal (validation, ``source_runs``/``source_signals``,
then consensus), so a push never bypasses scans, consensus or risk review.

* ``source_inbox_batches``: one row per accepted batch (source, received time,
  completeness, counts, who pushed it, diagnostics).
* ``source_inbox_signals``: the current signals of each inbox source, one row
  per ``(source_id, signal_id)``. Pushing the same signal again only refreshes
  it (idempotent on ``signal_id``). A ``complete`` batch withdraws the
  source's signals it does not contain.

:class:`InboxSource` is the base of the sources that read the inbox at scan
time (``push``, ``krr``): it returns the signals pushed within
``max_age_hours``. ``discover()`` is unavailable until something was pushed;
``health()`` is degraded when the newest push is older than ``max_age_hours``
(its signals are then no longer used).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Dict, List, Optional, Sequence

from pydantic import Field

from greenkube.signals.model import SourceSignal
from greenkube.signals.sdk import (
    CollectContext,
    Diagnostic,
    Source,
    SourceBatch,
    SourceDeps,
    SourceHealth,
    SourceLimits,
    SourceSettings,
)

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


#: Kinds whose targets the workload inventory knows (others are not checked against it).
_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Rollout"})


class InboxSettings(SourceSettings):
    """Settings of an inbox source (``SOURCES_CONFIG[<id>]``)."""

    #: Pushed signals older than this are no longer used.
    max_age_hours: float = Field(168.0, gt=0, le=720)


class InboxSource(Source):
    """Base of the sources that read pushed signals from the inbox at scan time."""

    settings_model: ClassVar[type[SourceSettings]] = InboxSettings
    limits: ClassVar[SourceLimits] = SourceLimits(probe_timeout_seconds=15.0, collect_timeout_seconds=60.0)
    #: Inbox rows read: one source id, or every source id under a prefix.
    inbox_source_id: ClassVar[Optional[str]] = None
    inbox_prefix: ClassVar[Optional[str]] = None
    #: How to push, for the "nothing received yet" reason.
    push_hint: ClassVar[str] = ""

    def __init__(self, settings: SourceSettings, deps: SourceDeps, inbox: Optional[SignalInboxRepository] = None):
        super().__init__(settings, deps)
        self._inbox = inbox

    @property
    def inbox(self) -> SignalInboxRepository:
        if self._inbox is None:
            self._inbox = create_signal_inbox_repository()
        return self._inbox

    @property
    def max_age(self) -> timedelta:
        hours = getattr(self.settings, "max_age_hours", InboxSettings().max_age_hours)
        return timedelta(hours=float(hours))

    async def _latest(self) -> Dict[str, InboxBatch]:
        cls = type(self)
        return await self.inbox.latest_batches(source_id=cls.inbox_source_id, source_prefix=cls.inbox_prefix)

    async def discover(self) -> SourceHealth:
        if not await self._latest():
            return SourceHealth.unavailable(f"nothing pushed yet; {type(self).push_hint}")
        return SourceHealth.available()

    async def health(self) -> SourceHealth:
        latest = await self._latest()
        if not latest:
            return SourceHealth.unavailable(f"nothing pushed yet; {type(self).push_hint}")
        newest = max(batch.received_at for batch in latest.values())
        age = self.deps.clock() - newest
        if age > self.max_age:
            hours = age.total_seconds() / 3600
            return SourceHealth.degraded(
                f"the newest push is {hours:.0f} h old, older than max_age_hours "
                f"({self.max_age.total_seconds() / 3600:g} h): its signals are no longer used"
            )
        return SourceHealth.available()

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        cls = type(self)
        stored = await self.inbox.current(
            source_id=cls.inbox_source_id,
            source_prefix=cls.inbox_prefix,
            since=self.deps.clock() - self.max_age,
            namespace=ctx.namespace,
        )
        signals: List[SourceSignal] = []
        other_cluster = unknown_targets = 0
        known = {(target.namespace, target.kind, target.name) for target in ctx.inventory.targets}
        for item in stored:
            signal = item.signal
            if signal.target.cluster_uid != ctx.cluster_uid:
                other_cluster += 1
                continue
            target = signal.target
            if known and target.kind in _WORKLOAD_KINDS and (target.namespace, target.kind, target.name) not in known:
                unknown_targets += 1
            signals.append(signal)
        diagnostics: List[Diagnostic] = []
        if other_cluster:
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="other_cluster",
                    message=f"pushed signals about another cluster than {ctx.cluster_uid!r} were ignored",
                    count=other_cluster,
                )
            )
        if unknown_targets:
            diagnostics.append(
                Diagnostic(
                    code="targets_without_usage",
                    message="signals about workloads GreenKube has no usage data for in this scan's window",
                    count=unknown_targets,
                )
            )
        return SourceBatch(signals=signals, complete=True, diagnostics=diagnostics)
