"""Isolated execution of sources (roadmap E5).

:class:`SourceRunner` runs one source and never raises (except on
cancellation): timeouts, exceptions, invalid signals, an open circuit or a rate
limit all end as a :class:`SourceRunRecord` with a status and an error code.
One failing source therefore never breaks a scan, and a source that did not
complete never makes its previous advice stale.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from greenkube.signals.model import MAX_RAW_BYTES, SourceSignal, missing_resource_value, raw_size, trim_raw
from greenkube.signals.sdk import (
    PLUGIN_ORIGIN,
    CollectContext,
    Diagnostic,
    Source,
    SourceBatch,
    SourceHealth,
    SourceHealthStatus,
    accepts_v1_recommendations,
    emits_source_id,
)

if TYPE_CHECKING:
    from greenkube.models.metrics import Recommendation
    from greenkube.models.optimization_run import SourceRunOutcome

logger = logging.getLogger(__name__)

#: Upper bound on the evidence summary and details a signal keeps (characters).
MAX_EVIDENCE_TEXT = 4096


class SourceRunStatus(StrEnum):
    """Outcome of one source within one scan."""

    #: Collected and reported a complete result.
    SUCCEEDED = "succeeded"
    #: Collected a partial result (or some signals were rejected).
    INCOMPLETE = "incomplete"
    #: Raised, timed out or returned something that is not a batch.
    FAILED = "failed"
    #: Not discovered or reported unavailable: not counted as a failure.
    UNAVAILABLE = "unavailable"
    #: Not attempted: rate limited or circuit open. Last known signals are kept.
    SKIPPED = "skipped"


class SourceRunRecord(BaseModel):
    """One source's run, persisted in ``source_runs``."""

    id: Optional[int] = None
    optimization_run_id: Optional[int] = None
    source_id: str
    source_version: Optional[str] = None
    #: Namespace the scan was restricted to; None for a cluster-wide scan.
    namespace: Optional[str] = None
    status: SourceRunStatus
    #: True only when the result is authoritative (``succeeded``).
    complete: bool = False
    health_status: Optional[SourceHealthStatus] = None
    health_reason: Optional[str] = None
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(0.0, ge=0)
    signal_count: int = Field(0, ge=0)
    recommendation_count: int = Field(0, ge=0)
    #: Failures in a row, including this run; drives the circuit breaker.
    consecutive_failures: int = Field(0, ge=0)
    #: When the source last actually collected (carried over by skipped runs); drives the rate limit.
    last_collected_at: Optional[datetime] = None
    #: ``timeout``, ``probe_timeout``, an exception type, ``rate_limited``, ``circuit_open``,
    #: ``not_discovered``, ``unhealthy``, ``invalid_batch``, ``rejected_signals``, ``incomplete``.
    error_code: Optional[str] = None
    #: API-safe message: never an exception message, which may carry credentials.
    error: Optional[str] = None
    diagnostics: List[Diagnostic] = Field(default_factory=list)


@dataclass
class SourceReport:
    """A run record plus what the source produced."""

    run: SourceRunRecord
    legacy_source: Optional[str] = None
    signals: List[SourceSignal] = field(default_factory=list)
    recommendations: List["Recommendation"] = field(default_factory=list)

    @property
    def source_id(self) -> str:
        return self.run.source_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _bounded_payloads(signal: SourceSignal) -> Optional[SourceSignal]:
    """``signal`` with its evidence and source estimate within bounds, or None when they already are.

    Storage keeps one row per distinct signal: a source must not be able to grow
    it without bound through free-form payloads. The identity is unchanged.
    """
    evidence = signal.evidence
    evidence_update: Dict[str, object] = {}
    if raw_size(evidence.data) > MAX_RAW_BYTES:
        evidence_update["data"] = trim_raw(evidence.data)
    for name in ("summary", "details"):
        text = getattr(evidence, name)
        if len(text) > MAX_EVIDENCE_TEXT:
            evidence_update[name] = text[: MAX_EVIDENCE_TEXT - 1] + "…"
    update: Dict[str, object] = {}
    if evidence_update:
        update["evidence"] = evidence.model_copy(update=evidence_update)
    if signal.source_estimate is not None and raw_size(signal.source_estimate) > MAX_RAW_BYTES:
        update["source_estimate"] = trim_raw(signal.source_estimate)
    return signal.model_copy(update=update) if update else None


def _finished(record: SourceRunRecord) -> datetime:
    return _aware(record.finished_at) or datetime.min.replace(tzinfo=timezone.utc)


class SourceRunner:
    """Runs sources with their limits; ``*_cap`` bound every source's own timeouts."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utcnow,
        probe_timeout_cap: Optional[float] = None,
        collect_timeout_cap: Optional[float] = None,
        on_failure: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.clock = clock
        self.probe_timeout_cap = probe_timeout_cap
        self.collect_timeout_cap = collect_timeout_cap
        self.on_failure = on_failure
        #: Last run per source, for callers without persisted run history.
        self.memory: Dict[str, SourceRunRecord] = {}

    def _bound(self, value: float, cap: Optional[float]) -> float:
        return min(value, cap) if cap is not None else value

    async def run_all(
        self,
        sources: Sequence[Source],
        ctx: CollectContext,
        previous: Optional[Dict[str, SourceRunRecord]] = None,
    ) -> List[SourceReport]:
        """Run sources one after the other (native analyzers share the scan context)."""
        history = dict(self.memory)
        for source_id, record in (previous or {}).items():
            # Keep the newer of the persisted and in-process runs: when recording
            # runs fails, the persisted one stops advancing and would otherwise
            # hide fresher failures from the circuit breaker.
            known = history.get(source_id)
            if known is None or _finished(record) >= _finished(known):
                history[source_id] = record
        reports = []
        for source in sources:
            report = await self.run(source, ctx, history.get(source.source_id))
            self.memory[source.source_id] = report.run
            reports.append(report)
        return reports

    async def run(
        self,
        source: Source,
        ctx: CollectContext,
        previous: Optional[SourceRunRecord] = None,
    ) -> SourceReport:
        cls = type(source)
        source_id = source.source_id
        limits = cls.limits
        started_at = self.clock()
        started = time.monotonic()
        # Only built-in sources feed the v1 pipeline and join a v1 source group.
        trusted_v1 = accepts_v1_recommendations(source)
        previous_failures = previous.consecutive_failures if previous is not None else 0
        last_collected_at = _aware(previous.last_collected_at) if previous is not None else None

        def finish(
            status: SourceRunStatus,
            *,
            error_code: Optional[str] = None,
            error: Optional[str] = None,
            health: Optional[SourceHealth] = None,
            signals: Optional[List[SourceSignal]] = None,
            recommendations: Optional[List["Recommendation"]] = None,
            diagnostics: Optional[List[Diagnostic]] = None,
            collected: bool = False,
        ) -> SourceReport:
            if status == SourceRunStatus.FAILED:
                failures = previous_failures + 1
            elif status == SourceRunStatus.SKIPPED:
                failures = previous_failures
            else:
                failures = 0
            record = SourceRunRecord(
                source_id=source_id,
                source_version=cls.version,
                namespace=ctx.namespace,
                status=status,
                complete=status == SourceRunStatus.SUCCEEDED,
                health_status=health.status if health is not None else None,
                health_reason=health.reason if health is not None else None,
                started_at=started_at,
                finished_at=self.clock(),
                duration_seconds=round(max(time.monotonic() - started, 0.0), 3),
                signal_count=len(signals or []),
                recommendation_count=len(recommendations or []),
                consecutive_failures=failures,
                last_collected_at=started_at if collected else last_collected_at,
                error_code=error_code,
                error=error,
                diagnostics=list(diagnostics or []),
            )
            return SourceReport(
                run=record,
                legacy_source=cls.legacy_source if trusted_v1 else None,
                signals=list(signals or []),
                recommendations=list(recommendations or []),
            )

        # Circuit breaker: skip a source that keeps failing until its cooldown passed.
        if previous is not None and previous_failures >= limits.failure_threshold:
            last_end = _aware(previous.finished_at) or started_at
            if started_at - last_end < timedelta(seconds=limits.circuit_cooldown_seconds):
                logger.warning(
                    "Source '%s' skipped: %d consecutive failures (circuit open).", source_id, previous_failures
                )
                return finish(
                    SourceRunStatus.SKIPPED,
                    error_code="circuit_open",
                    error=f"{previous_failures} consecutive failures; retried after the cooldown",
                )
        # Rate limit: keep the last known signals between two collections.
        if limits.min_interval_seconds > 0 and last_collected_at is not None:
            if started_at - last_collected_at < timedelta(seconds=limits.min_interval_seconds):
                logger.debug("Source '%s' skipped: rate limited.", source_id)
                return finish(SourceRunStatus.SKIPPED, error_code="rate_limited", error="rate limited")

        probe_timeout = self._bound(limits.probe_timeout_seconds, self.probe_timeout_cap)
        try:
            health = await asyncio.wait_for(self._probe(source), timeout=probe_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.error("Source '%s' probe timed out; continuing without it.", source_id)
            self._failed(source_id)
            return finish(SourceRunStatus.FAILED, error_code="probe_timeout", error="timeout")
        except Exception as exc:
            logger.exception("Source '%s' probe failed; continuing without it.", source_id)
            self._failed(source_id)
            return finish(SourceRunStatus.FAILED, error_code=type(exc).__name__, error=type(exc).__name__)
        if health.status == SourceHealthStatus.UNAVAILABLE:
            logger.debug("Source '%s' is not available (%s); skipping.", source_id, health.reason)
            return finish(SourceRunStatus.UNAVAILABLE, error_code="not_discovered", health=health)

        collect_timeout = self._bound(limits.collect_timeout_seconds, self.collect_timeout_cap)
        try:
            batch = await asyncio.wait_for(source.collect(ctx), timeout=collect_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.error("Source '%s' timed out; continuing without it.", source_id)
            self._failed(source_id)
            return finish(SourceRunStatus.FAILED, error_code="timeout", error="timeout", health=health)
        except Exception as exc:
            logger.exception("Source '%s' failed; continuing without it.", source_id)
            self._failed(source_id)
            return finish(
                SourceRunStatus.FAILED, error_code=type(exc).__name__, error=type(exc).__name__, health=health
            )
        if not isinstance(batch, SourceBatch):
            logger.error("Source '%s' returned %s instead of a SourceBatch.", source_id, type(batch).__name__)
            self._failed(source_id)
            return finish(SourceRunStatus.FAILED, error_code="invalid_batch", error="invalid batch", health=health)

        signals, diagnostics, rejected = self._validate(source, batch)
        recommendations = list(batch.recommendations or [])
        if recommendations and not trusted_v1:
            # FAIL CLOSED: third-party advice never bypasses signals, consensus and
            # risk review to reach the v1 pipeline (persistence, pull requests).
            logger.warning(
                "Source '%s' returned %d v1 recommendations; ignored (only signals are accepted).",
                source_id,
                len(recommendations),
            )
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="recommendations_ignored",
                    message="v1 recommendations are accepted from built-in sources only; emit signals instead",
                    count=len(recommendations),
                )
            )
            recommendations = []
        complete = batch.complete and not rejected
        if complete:
            return finish(
                SourceRunStatus.SUCCEEDED,
                health=health,
                signals=signals,
                recommendations=recommendations,
                diagnostics=diagnostics,
                collected=True,
            )
        logger.warning("Source '%s' returned an incomplete result.", source_id)
        return finish(
            SourceRunStatus.INCOMPLETE,
            error_code="rejected_signals" if rejected else "incomplete",
            error="incomplete result",
            health=health,
            signals=signals,
            recommendations=recommendations,
            diagnostics=diagnostics,
            collected=True,
        )

    @staticmethod
    async def _probe(source: Source) -> SourceHealth:
        discovered = await source.discover()
        if discovered.status == SourceHealthStatus.UNAVAILABLE:
            return discovered
        health = await source.health()
        if health.version is None and discovered.version is not None:
            health = health.model_copy(update={"version": discovered.version})
        return health

    def _validate(self, source: Source, batch: SourceBatch) -> tuple[List[SourceSignal], List[Diagnostic], bool]:
        """Drop signals that break the contract; every drop is a diagnostic and makes the batch incomplete."""
        cls = type(source)
        diagnostics = list(batch.diagnostics)
        kept: List[SourceSignal] = []
        seen: set[str] = set()
        foreign = outside = duplicates = trimmed = bounded = invalid = reserved = valueless = 0
        plugin = getattr(source, "registry_origin", None) == PLUGIN_ORIGIN
        for signal in batch.signals:
            if not isinstance(signal, SourceSignal):
                invalid += 1
                continue
            if not emits_source_id(cls, signal.source_id):
                foreign += 1
                continue
            if signal.capability not in cls.capabilities:
                outside += 1
                continue
            if plugin and missing_resource_value(signal.capability, signal.proposal, signal.kind) is not None:
                # A plugin's request or limit signal without a value would turn the consensus categorical
                # (built-in v1 connectors keep their legacy advisory rows; consensus drops such a ballot).
                valueless += 1
                continue
            if plugin and _reserved_engine_family(signal):
                # A resolved ``greenkube.*`` engine family counts as GreenKube's own measurement (A.5).
                reserved += 1
                continue
            if signal.signal_id in seen:
                duplicates += 1
                continue
            if raw_size(signal.raw) > MAX_RAW_BYTES:
                signal = signal.model_copy(update={"raw": trim_raw(signal.raw)})
                trimmed += 1
            within_bounds = _bounded_payloads(signal)
            if within_bounds is not None:
                signal = within_bounds
                bounded += 1
            seen.add(signal.signal_id)
            kept.append(signal)
        rejected = False
        for count, code, message in (
            (invalid, "invalid_signal", "items that are not SourceSignal objects were dropped"),
            (foreign, "foreign_source_id", "signals carrying another source id were dropped"),
            (outside, "undeclared_capability", "signals outside the declared capabilities were dropped"),
            (reserved, "reserved_engine_family", "signals declaring a greenkube engine family were dropped"),
            (
                valueless,
                "missing_resource_value",
                "container requests or limits signals proposing no request or limit value were dropped",
            ),
        ):
            if count:
                rejected = True
                diagnostics.append(Diagnostic(level="error", code=code, message=message, count=count))
        if len(kept) > cls.limits.max_signals:
            rejected = True
            dropped = len(kept) - cls.limits.max_signals
            kept = kept[: cls.limits.max_signals]
            diagnostics.append(
                Diagnostic(
                    level="error", code="max_signals", message="signals over the limit were dropped", count=dropped
                )
            )
        # Informational only: they do not make the batch incomplete.
        if duplicates:
            diagnostics.append(
                Diagnostic(code="duplicate_signal", message="identical signals were merged", count=duplicates)
            )
        if trimmed:
            diagnostics.append(
                Diagnostic(code="raw_trimmed", message="raw payloads over 8 KiB were trimmed", count=trimmed)
            )
        if bounded:
            diagnostics.append(
                Diagnostic(
                    code="evidence_trimmed",
                    message="evidence or source estimates over the size bounds were trimmed",
                    count=bounded,
                )
            )
        return kept, diagnostics, rejected

    def _failed(self, source_id: str) -> None:
        if self.on_failure is not None:
            try:
                self.on_failure(source_id)
            except Exception:
                logger.debug("Failure hook raised for source '%s'.", source_id, exc_info=True)


_ERROR_STATUSES = (SourceRunStatus.FAILED, SourceRunStatus.INCOMPLETE, SourceRunStatus.SKIPPED)


def legacy_outcomes(reports: Sequence[SourceReport]) -> List["SourceRunOutcome"]:
    """v1 per-source outcomes (``optimization_runs.source_outcomes``), one per v1 source.

    Native analyzers are separate sources but share the v1 source ``greenkube``:
    the group is complete only when all of them completed, so a failed analyzer
    never lets reconciliation mark native recommendations stale (E8).
    """
    from greenkube.models.optimization_run import SourceRunOutcome
    from greenkube.models.optimization_run import SourceRunStatus as V1Status

    groups: Dict[str, List[SourceReport]] = {}
    for report in reports:
        groups.setdefault(report.legacy_source or report.source_id, []).append(report)

    outcomes: List[SourceRunOutcome] = []
    for name, members in groups.items():
        runs = [member.run for member in members]
        attempted = [run for run in runs if run.status != SourceRunStatus.UNAVAILABLE]
        if not attempted:
            status = V1Status.UNAVAILABLE
        elif all(run.status == SourceRunStatus.SUCCEEDED for run in attempted):
            status = V1Status.SUCCEEDED
        elif all(run.status == SourceRunStatus.FAILED for run in attempted):
            status = V1Status.FAILED
        else:
            status = V1Status.INCOMPLETE
        failing = [run for run in runs if run.status in _ERROR_STATUSES]
        if len(runs) == 1:
            error = runs[0].error
        else:
            error = "; ".join(f"{run.source_id}: {run.error or run.status.value}" for run in failing) or None
        outcomes.append(
            SourceRunOutcome(
                source=name,
                status=status,
                complete=status == V1Status.SUCCEEDED,
                recommendation_count=sum(len(member.recommendations) for member in members),
                duration_seconds=round(sum(run.duration_seconds for run in runs), 3),
                error=error if status != V1Status.SUCCEEDED or failing else None,
            )
        )
    return outcomes


def _reserved_engine_family(signal: SourceSignal) -> bool:
    """Whether a signal resolves to an engine family reserved for GreenKube's own measurement.

    Uses the family the consensus resolves (the signal's ``engine_family``, the
    algorithm's attribute or ``params["engine_family"]``...), so no spelling of
    ``greenkube`` / ``greenkube.*`` escapes the check.
    """
    from greenkube.signals.consensus import engine_family

    text = engine_family(signal).strip().lower()
    return text == "greenkube" or text.startswith("greenkube.")
