"""In-memory signal inbox with the repository's semantics, for source and API tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from greenkube.signals.connectors.inbox import InboxBatch, InboxSignal, SignalInboxRepository


class MemoryInbox(SignalInboxRepository):
    def __init__(self) -> None:
        self.signals: Dict[Tuple[str, str], InboxSignal] = {}
        self.batches: List[InboxBatch] = []

    def seed(self, batch: InboxBatch, signals) -> None:
        """Store a batch synchronously (for fixtures built outside an event loop)."""
        for signal in signals:
            self.signals[(batch.source_id, signal.signal_id)] = InboxSignal(
                signal=signal, batch_id=batch.batch_id, received_at=batch.received_at
            )
        self.batches.append(batch)

    async def accept(self, batch, signals):
        for signal in signals:
            self.signals[(batch.source_id, signal.signal_id)] = InboxSignal(
                signal=signal, batch_id=batch.batch_id, received_at=batch.received_at
            )
        withdrawn = 0
        if batch.complete:
            stale = [
                key
                for key, item in self.signals.items()
                if key[0] == batch.source_id and item.batch_id != batch.batch_id
            ]
            for key in stale:
                del self.signals[key]
            withdrawn = len(stale)
        stored = replace(batch, withdrawn=withdrawn)
        self.batches.append(stored)
        return stored

    async def count(self, source_id):
        return sum(1 for key in self.signals if key[0] == source_id)

    @staticmethod
    def _matches(source: str, source_id: Optional[str], source_prefix: Optional[str]) -> bool:
        return source == source_id if source_id is not None else source.startswith(source_prefix or "")

    async def current(self, *, source_id=None, source_prefix=None, since: Optional[datetime] = None, namespace=None):
        return [
            item
            for (source, _signal_id), item in sorted(self.signals.items())
            if self._matches(source, source_id, source_prefix)
            and (since is None or item.received_at >= since)
            and (namespace is None or item.signal.target.namespace == namespace)
        ]

    async def latest_batches(self, *, source_id=None, source_prefix=None):
        latest: Dict[str, InboxBatch] = {}
        for batch in self.batches:
            if self._matches(batch.source_id, source_id, source_prefix):
                known = latest.get(batch.source_id)
                if known is None or batch.received_at >= known.received_at:
                    latest[batch.source_id] = batch
        return latest

    async def prune(self, before):
        stale = [key for key, item in self.signals.items() if item.received_at < before]
        for key in stale:
            del self.signals[key]
        kept = [batch for batch in self.batches if batch.received_at >= before]
        removed = len(self.batches) - len(kept) + len(stale)
        self.batches = kept
        return removed
