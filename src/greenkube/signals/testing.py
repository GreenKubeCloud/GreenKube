"""Contract checks every source must pass (roadmap E5).

Third-party authors run them from their own test suite::

    from greenkube.signals.testing import check_source_contract

    async def test_my_source_honours_the_contract():
        await check_source_contract(MySource(MySource.settings_model(), deps), ctx, empty_ctx)

They raise ``AssertionError`` with an explanation on the first violation. The
built-in sources run them in ``tests/sources``.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from greenkube.models.metrics import RecommendationRecord
from greenkube.signals.mapping import SOURCE_FIELDS, recommendation_from_signal
from greenkube.signals.model import MAX_RAW_BYTES, SourceSignal, raw_size
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import (
    CollectContext,
    Source,
    SourceBatch,
    SourceHealth,
    SourceHealthStatus,
    emits_source_id,
    validate_source_class,
)


def check_source_class(cls: type[Source], *, builtin: bool = False) -> None:
    """Metadata: id, display name, tier, capabilities, limits and settings model."""
    try:
        validate_source_class(cls, builtin=builtin)
    except ValueError as exc:
        raise AssertionError(str(exc)) from exc
    try:
        cls.settings_model.model_validate({})
    except Exception as exc:
        raise AssertionError(f"{cls.id}: settings must have defaults for every field ({exc})") from exc


def check_signal(source: Source, signal: SourceSignal, ctx: CollectContext) -> None:
    """One signal: identity, declared capability, bounded raw payload and a stable JSON round trip."""
    cls = type(source)
    assert isinstance(signal, SourceSignal), f"{cls.id}: {type(signal).__name__} is not a SourceSignal"
    assert emits_source_id(cls, signal.source_id), f"{cls.id}: signal carries source_id {signal.source_id!r}"
    declared = sorted(c.value for c in cls.capabilities)
    assert signal.capability in cls.capabilities, (
        f"{cls.id}: capability {signal.capability.value!r} is not declared in {declared}"
    )
    assert signal.target.cluster_uid == ctx.cluster_uid, f"{cls.id}: target is not qualified by the scan's cluster"
    assert raw_size(signal.raw) <= MAX_RAW_BYTES, f"{cls.id}: raw payload exceeds {MAX_RAW_BYTES} bytes"
    assert signal.fetched_at.tzinfo is not None, f"{cls.id}: fetched_at must be timezone-aware"
    assert signal.algorithm.name, f"{cls.id}: algorithm.name is required"
    copy = SourceSignal.model_validate_json(signal.model_dump_json())
    assert copy == signal, f"{cls.id}: signal does not survive a JSON round trip"
    assert copy.signal_id == signal.signal_id, f"{cls.id}: signal_id is not deterministic"
    if ctx.namespace is not None and signal.target.namespace is not None:
        assert signal.target.namespace == ctx.namespace, f"{cls.id}: signal outside the scan's namespace"


def check_v1_round_trip(source: Source, batch: SourceBatch) -> None:
    """Sources that feed the v1 pipeline: every recommendation maps to a signal and back losslessly."""
    cls = type(source)
    if not batch.recommendations:
        return
    assert cls.legacy_source, f"{cls.id}: returns v1 recommendations but declares no legacy_source"
    by_fingerprint: dict = {}
    for signal in batch.signals:
        by_fingerprint.setdefault(signal.provenance.legacy_fingerprint, []).append(signal)
    for rec in batch.recommendations:
        assert str(getattr(rec.source, "value", rec.source)) == cls.legacy_source, (
            f"{cls.id}: recommendation source {rec.source!r} differs from legacy_source {cls.legacy_source!r}"
        )
        fingerprint = RecommendationRecord.from_recommendation(rec).fingerprint
        signals = by_fingerprint.get(fingerprint)
        assert signals, f"{cls.id}: recommendation {fingerprint} has no signal"
        # Several signals may support one recommendation (two VPAs agreeing on
        # one container): one of them must reproduce it exactly.
        expected = rec.model_dump(mode="json", include=set(SOURCE_FIELDS))
        rebuilt = [
            recommendation_from_signal(signal).model_dump(mode="json", include=set(SOURCE_FIELDS)) for signal in signals
        ]
        assert expected in rebuilt, f"{cls.id}: v1 -> v2 -> v1 changes {_diff(expected, rebuilt[0])}"


def _diff(expected: dict, rebuilt: dict) -> List[str]:
    return sorted(name for name in expected if expected.get(name) != rebuilt.get(name))


async def check_source_contract(
    source: Source,
    ctx: CollectContext,
    empty_ctx: Optional[CollectContext] = None,
    *,
    builtin: bool = False,
    expect_signals: bool = True,
) -> SourceBatch:
    """Run the whole contract on ``source`` and return its batch for ``ctx``.

    * metadata is valid and settings have defaults;
    * ``discover()``/``health()`` return a :class:`SourceHealth` within the probe timeout;
    * ``collect()`` returns a :class:`SourceBatch` within the collect timeout, with
      valid signals (``expect_signals`` requires at least one for ``ctx``);
    * collecting twice yields the same signal ids (deterministic identity);
    * v1 recommendations, if any, round-trip through their signals;
    * an empty context yields a batch without raising;
    * the :class:`SourceRunner` records the run as succeeded.
    """
    cls = type(source)
    check_source_class(cls, builtin=builtin)
    assert source.source_id == cls.id, f"{cls.id}: instance id differs from the class id"

    discovered = await asyncio.wait_for(source.discover(), timeout=cls.limits.probe_timeout_seconds)
    assert isinstance(discovered, SourceHealth), f"{cls.id}: discover() must return SourceHealth"
    assert discovered.status != SourceHealthStatus.UNAVAILABLE, (
        f"{cls.id}: discover() reports unavailable ({discovered.reason}); run the contract with its prerequisites"
    )
    health = await asyncio.wait_for(source.health(), timeout=cls.limits.probe_timeout_seconds)
    assert isinstance(health, SourceHealth), f"{cls.id}: health() must return SourceHealth"

    batch = await asyncio.wait_for(source.collect(ctx), timeout=cls.limits.collect_timeout_seconds)
    assert isinstance(batch, SourceBatch), f"{cls.id}: collect() must return a SourceBatch"
    if expect_signals:
        assert batch.signals, f"{cls.id}: expected at least one signal for the contract context"
    for signal in batch.signals:
        check_signal(source, signal, ctx)
    check_v1_round_trip(source, batch)

    again = await asyncio.wait_for(source.collect(ctx), timeout=cls.limits.collect_timeout_seconds)
    assert {s.signal_id for s in again.signals} == {s.signal_id for s in batch.signals}, (
        f"{cls.id}: the same input must produce the same signal ids"
    )

    if empty_ctx is not None:
        empty = await asyncio.wait_for(source.collect(empty_ctx), timeout=cls.limits.collect_timeout_seconds)
        assert isinstance(empty, SourceBatch), f"{cls.id}: collect() on an empty context must return a SourceBatch"
        for signal in empty.signals:
            check_signal(source, signal, empty_ctx)

    report = await SourceRunner().run(source, ctx)
    assert report.run.status == SourceRunStatus.SUCCEEDED, (
        f"{cls.id}: the runner recorded {report.run.status.value} ({report.run.error_code}, "
        f"{[d.code for d in report.run.diagnostics]})"
    )
    assert report.run.signal_count == len(batch.signals), f"{cls.id}: the runner dropped signals"
    return batch
