"""Sources that replay pushed signals into scans (roadmap G2, G6).

Pushed signals are stored in the inbox (:mod:`~greenkube.signals.connectors.inbox`)
when they arrive. At scan time an :class:`InboxSource` returns the signals
pushed within ``max_age_hours``, so they are validated by the source runner,
recorded in ``source_runs``/``source_signals`` and reach recommendations only
through consensus, like the signals of any other source.

* ``discover()``: unavailable until something was pushed (nothing to run);
* ``health()``: degraded when the newest push is older than ``max_age_hours``
  (its signals are then no longer replayed: the source reports no advice).
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar, Dict, FrozenSet, List, Optional

from pydantic import Field

from greenkube.signals.connectors.inbox import InboxBatch, SignalInboxRepository, create_signal_inbox_repository
from greenkube.signals.connectors.push import PUSH_PREFIX
from greenkube.signals.model import CLUSTER_SCOPED_KINDS, Capability, SourceSignal
from greenkube.signals.sdk import (
    CollectContext,
    Diagnostic,
    Source,
    SourceBatch,
    SourceDeps,
    SourceHealth,
    SourceLimits,
    SourceSettings,
    SourceTier,
)

#: Kinds whose targets the workload inventory knows (others are not checked against it).
_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Rollout"})


class InboxSettings(SourceSettings):
    """Settings of an inbox source (``SOURCES_CONFIG[<id>]``)."""

    #: Pushed signals older than this are no longer replayed.
    max_age_hours: float = Field(168.0, gt=0, le=720)


class InboxSource(Source):
    """Base of the sources that replay pushed signals."""

    settings_model: ClassVar[type[SourceSettings]] = InboxSettings
    limits: ClassVar[SourceLimits] = SourceLimits(probe_timeout_seconds=15.0, collect_timeout_seconds=60.0)
    #: Inbox rows replayed: one source id, or every source id under a prefix.
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
            if (
                known
                and target.kind in _WORKLOAD_KINDS
                and target.kind not in CLUSTER_SCOPED_KINDS
                and (target.namespace, target.kind, target.name) not in known
            ):
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


class PushSignalSource(InboxSource):
    """Signals pushed by any tool through ``POST /api/v1/signals`` (roadmap G6)."""

    id: ClassVar[str] = "push"
    display_name: ClassVar[str] = "Push API"
    tier: ClassVar[SourceTier] = SourceTier.CUSTOM
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset(Capability)
    also_emits: ClassVar[FrozenSet[str]] = frozenset({PUSH_PREFIX})
    description: ClassVar[str] = (
        "Signals pushed by external tools to POST /api/v1/signals (source ids push:<client>); "
        "they become recommendations only through consensus."
    )
    inbox_prefix: ClassVar[Optional[str]] = PUSH_PREFIX
    push_hint: ClassVar[str] = "POST a signal batch to /api/v1/signals with a signals:write API key"
