"""SourceRunner isolation: timeouts, rate limit, circuit breaker and signal validation (roadmap E5)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import ClassVar, FrozenSet, List, Optional

from greenkube.core.config import Config
from greenkube.models.optimization_run import SourceRunStatus as V1Status
from greenkube.signals.model import (
    MAX_RAW_BYTES,
    AlgorithmInfo,
    Capability,
    DeletionProposal,
    SourceSignal,
    TargetRef,
)
from greenkube.signals.runtime import (
    SourceReport,
    SourceRunner,
    SourceRunRecord,
    SourceRunStatus,
    legacy_outcomes,
)
from greenkube.signals.sdk import (
    CollectContext,
    Source,
    SourceBatch,
    SourceDeps,
    SourceHealth,
    SourceHealthStatus,
    SourceLimits,
    SourceSettings,
)

CLUSTER = "c1"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def _signal(source_id: str = "acme", name: str = "pv-1", capability: Capability = Capability.CLEANUP, raw=None):
    return SourceSignal(
        source_id=source_id,
        target=TargetRef.for_kind(CLUSTER, "PersistentVolume", name),
        capability=capability,
        kind="ORPHANED_PERSISTENT_VOLUME",
        proposal=DeletionProposal(reason="Released"),
        algorithm=AlgorithmInfo(name="acme.cleanup"),
        raw=raw or {},
    )


class AcmeSource(Source):
    id: ClassVar[str] = "acme"
    display_name: ClassVar[str] = "Acme"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CLEANUP})
    limits: ClassVar[SourceLimits] = SourceLimits(
        min_interval_seconds=0, failure_threshold=2, circuit_cooldown_seconds=600
    )

    def __init__(self, signals=None, *, error=None, delay=0.0, discovered=True, health=None, complete=True):
        super().__init__(SourceSettings(), SourceDeps(config=Config(), cluster_uid=CLUSTER))
        self.signals: List = list(signals or [])
        self.error = error
        self.delay = delay
        self.discovered = discovered
        self._health = health
        self.complete = complete
        self.calls = 0

    async def discover(self) -> SourceHealth:
        if self.discovered:
            return SourceHealth.available(version="1.2.3")
        return SourceHealth.unavailable("CRD acme.io not installed")

    async def health(self) -> SourceHealth:
        return self._health or SourceHealth.available()

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return SourceBatch(signals=list(self.signals), complete=self.complete)


class RateLimitedSource(AcmeSource):
    id: ClassVar[str] = "acme-slow"
    limits: ClassVar[SourceLimits] = SourceLimits(min_interval_seconds=3600)


def _ctx() -> CollectContext:
    return CollectContext(cluster_uid=CLUSTER, namespace=None, window=None, deadline=datetime.now(timezone.utc))


async def _run(source: Source, previous: Optional[SourceRunRecord] = None, **runner) -> SourceReport:
    return await SourceRunner(**runner).run(source, _ctx(), previous)


class TestOutcomes:
    async def test_success_records_counts_health_and_collection_time(self):
        clock = Clock()
        report = await _run(AcmeSource([_signal()]), clock=clock)
        run = report.run
        assert run.status == SourceRunStatus.SUCCEEDED and run.complete
        assert run.signal_count == 1
        assert run.health_status == SourceHealthStatus.AVAILABLE
        assert run.last_collected_at == clock.now
        assert run.consecutive_failures == 0

    async def test_not_discovered_is_unavailable_not_failed(self):
        source = AcmeSource([_signal()], discovered=False)
        report = await _run(source)
        assert report.run.status == SourceRunStatus.UNAVAILABLE
        assert report.run.health_reason == "CRD acme.io not installed"
        assert report.run.consecutive_failures == 0
        assert source.calls == 0

    async def test_degraded_health_still_collects(self):
        report = await _run(AcmeSource([_signal()], health=SourceHealth.degraded("slow API")))
        assert report.run.status == SourceRunStatus.SUCCEEDED
        assert report.run.health_status == SourceHealthStatus.DEGRADED

    async def test_exception_is_recorded_without_its_message(self):
        failures: List[str] = []
        report = await _run(AcmeSource(error=ConnectionError("token=s3cr3t")), on_failure=failures.append)
        assert report.run.status == SourceRunStatus.FAILED
        assert report.run.error_code == "ConnectionError"
        assert "s3cr3t" not in (report.run.error or "")
        assert report.run.consecutive_failures == 1
        assert failures == ["acme"]

    async def test_timeout_is_bounded_by_the_engine_cap(self):
        report = await _run(AcmeSource(delay=5), collect_timeout_cap=0.01)
        assert report.run.status == SourceRunStatus.FAILED
        assert report.run.error_code == "timeout"

    async def test_partial_batch_is_incomplete(self):
        report = await _run(AcmeSource([_signal()], complete=False))
        assert report.run.status == SourceRunStatus.INCOMPLETE
        assert not report.run.complete
        assert report.signals  # partial signals are kept, never authoritative

    async def test_a_non_batch_result_fails(self):
        class Broken(AcmeSource):
            async def collect(self, ctx):  # pyrefly: ignore[bad-override]
                return ["not a batch"]

        report = await _run(Broken())
        assert report.run.status == SourceRunStatus.FAILED
        assert report.run.error_code == "invalid_batch"


class TestSignalValidation:
    async def test_contract_violations_are_dropped_and_make_the_batch_incomplete(self):
        signals = [
            _signal(),
            _signal(source_id="someone-else", name="pv-2"),
            _signal(name="pv-3", capability=Capability.CONTAINER_REQUESTS),
            "garbage",
        ]
        report = await _run(AcmeSource(signals))
        assert report.run.status == SourceRunStatus.INCOMPLETE
        assert report.run.error_code == "rejected_signals"
        assert [s.target.name for s in report.signals] == ["pv-1"]
        assert {d.code for d in report.run.diagnostics} == {
            "foreign_source_id",
            "undeclared_capability",
            "invalid_signal",
        }

    async def test_duplicates_and_large_raw_payloads_are_normalized(self):
        big = _signal(name="pv-big", raw={"blob": "x" * (MAX_RAW_BYTES * 2)})
        report = await _run(AcmeSource([_signal(), _signal(), big]))
        assert report.run.status == SourceRunStatus.SUCCEEDED
        assert report.run.signal_count == 2
        trimmed = next(s for s in report.signals if s.target.name == "pv-big")
        assert trimmed.raw["truncated"] is True
        assert {d.code for d in report.run.diagnostics} == {"duplicate_signal", "raw_trimmed"}

    async def test_signals_over_the_limit_are_dropped(self):
        class Tiny(AcmeSource):
            limits: ClassVar[SourceLimits] = SourceLimits(max_signals=1)

        report = await _run(Tiny([_signal(name="a"), _signal(name="b")]))
        assert report.run.status == SourceRunStatus.INCOMPLETE
        assert report.run.signal_count == 1


class TestCircuitBreakerAndRateLimit:
    async def test_circuit_opens_after_consecutive_failures_and_retries_after_cooldown(self):
        clock = Clock()
        source = AcmeSource(error=RuntimeError("down"))
        runner = SourceRunner(clock=clock)

        first = await runner.run(source, _ctx())
        second = await runner.run(source, _ctx(), first.run)
        assert second.run.consecutive_failures == 2

        clock.advance(minutes=1)
        skipped = await runner.run(source, _ctx(), second.run)
        assert skipped.run.status == SourceRunStatus.SKIPPED
        assert skipped.run.error_code == "circuit_open"
        assert source.calls == 2

        clock.advance(minutes=11)
        source.error = None
        source.signals = [_signal()]
        recovered = await runner.run(source, _ctx(), skipped.run)
        assert recovered.run.status == SourceRunStatus.SUCCEEDED
        assert recovered.run.consecutive_failures == 0

    async def test_rate_limit_skips_until_the_interval_passed(self):
        clock = Clock()
        source = RateLimitedSource([_signal(source_id="acme-slow")])
        runner = SourceRunner(clock=clock)

        first = await runner.run(source, _ctx())
        clock.advance(minutes=30)
        skipped = await runner.run(source, _ctx(), first.run)
        assert skipped.run.status == SourceRunStatus.SKIPPED
        assert skipped.run.error_code == "rate_limited"
        assert skipped.run.last_collected_at == first.run.started_at

        clock.advance(minutes=31)
        again = await runner.run(source, _ctx(), skipped.run)
        assert again.run.status == SourceRunStatus.SUCCEEDED
        assert source.calls == 2

    async def test_in_process_history_is_used_without_persisted_runs(self):
        clock = Clock()
        runner = SourceRunner(clock=clock)
        source = RateLimitedSource([_signal(source_id="acme-slow")])
        await runner.run_all([source], _ctx())
        [report] = await runner.run_all([source], _ctx())
        assert report.run.status == SourceRunStatus.SKIPPED


def _report(
    source_id: str, status: SourceRunStatus, legacy: Optional[str], error: Optional[str] = None
) -> SourceReport:
    now = datetime.now(timezone.utc)
    run = SourceRunRecord(source_id=source_id, status=status, started_at=now, finished_at=now, error=error)
    return SourceReport(run=run, legacy_source=legacy)


class TestLegacyOutcomes:
    def test_native_analyzers_form_one_v1_source(self):
        outcomes = legacy_outcomes(
            [
                _report("vpa", SourceRunStatus.UNAVAILABLE, "vpa"),
                _report("greenkube.rightsizing_cpu", SourceRunStatus.SUCCEEDED, "greenkube"),
                _report("greenkube.orphaned_lb", SourceRunStatus.FAILED, "greenkube", "RuntimeError"),
                _report("acme", SourceRunStatus.SUCCEEDED, None),
            ]
        )
        assert [(o.source, o.status) for o in outcomes] == [
            ("vpa", V1Status.UNAVAILABLE),
            # One failed analyzer makes the whole native source non-authoritative:
            # none of its recommendations may go stale this run.
            ("greenkube", V1Status.INCOMPLETE),
            ("acme", V1Status.SUCCEEDED),
        ]
        assert outcomes[1].error == "greenkube.orphaned_lb: RuntimeError"
        assert not outcomes[1].complete

    def test_single_source_keeps_its_error_text(self):
        [outcome] = legacy_outcomes([_report("vpa", SourceRunStatus.FAILED, "vpa", "timeout")])
        assert outcome.status == V1Status.FAILED and outcome.error == "timeout"
        [skipped] = legacy_outcomes([_report("vpa", SourceRunStatus.SKIPPED, "vpa", "rate limited")])
        assert skipped.status == V1Status.INCOMPLETE
