"""Reusable contract suite for Source SDK sources (roadmap E5).

Subclass :class:`SourceContract` in a ``Test*`` class and implement
``make_source``, ``make_context`` and ``make_empty_context``; pytest collects
the inherited tests. The checks themselves live in
:mod:`greenkube.signals.testing` so third-party packages can run them too.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.signals.adapters import observed_window, workload_inventory
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import CollectContext, Source, SourceDeps, SourceLimits
from greenkube.signals.testing import check_source_contract

CLUSTER = "contract-cluster"


def deps(config: Optional[Config] = None) -> SourceDeps:
    return SourceDeps(config=config or Config(), cluster_uid=CLUSTER)


def collect_context(context: Optional[OptimizationContext], namespace: Optional[str] = None) -> CollectContext:
    return CollectContext(
        cluster_uid=CLUSTER,
        namespace=namespace,
        window=observed_window(context),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        inventory=workload_inventory(context, CLUSTER),
        optimization=context,
    )


class SourceContract:
    """Inherited contract tests; subclasses provide the source and its contexts."""

    #: Built-in sources may use the reserved ``greenkube.`` prefix.
    builtin: bool = True
    #: The contract context must produce at least one signal.
    expect_signals: bool = True

    def make_source(self) -> Source:
        raise NotImplementedError

    def make_context(self) -> CollectContext:
        raise NotImplementedError

    def make_empty_context(self) -> CollectContext:
        return collect_context(OptimizationContext(config=Config()))

    async def test_honours_the_source_contract(self):
        await check_source_contract(
            self.make_source(),
            self.make_context(),
            self.make_empty_context(),
            builtin=self.builtin,
            expect_signals=self.expect_signals,
        )

    async def test_a_raising_collect_is_recorded_not_raised(self):
        source = self.make_source()

        async def broken(ctx):
            raise RuntimeError("secret token abc123 in the message")

        source.collect = broken  # type: ignore[method-assign]
        report = await SourceRunner().run(source, self.make_context())

        assert report.run.status == SourceRunStatus.FAILED
        assert report.run.error_code == "RuntimeError"
        assert "abc123" not in (report.run.error or "")
        assert report.signals == [] and report.recommendations == []

    async def test_a_hung_collect_times_out(self):
        source = self.make_source()

        async def hung(ctx):
            await asyncio.sleep(10)

        source.collect = hung  # type: ignore[method-assign]
        report = await SourceRunner(collect_timeout_cap=0.01).run(source, self.make_context())

        assert report.run.status == SourceRunStatus.FAILED
        assert report.run.error_code == "timeout"

    def test_declares_bounded_limits(self):
        limits = type(self.make_source()).limits
        assert isinstance(limits, SourceLimits)
        assert limits.collect_timeout_seconds <= 3600
        assert limits.probe_timeout_seconds <= 300
