"""A native OOM-driven memory increase is a lower bound of the consensus.

The OOM kill proves the container needs more than its limit: no external
ballot (VPA, KRR, a pushed or plugin signal) may bring the consolidated value
below GreenKube's OOM-driven increase. Before the fix the analyzer's
``oom_kills`` trigger only reached the recommendation as reason text: under the
former ``priority``, ``median`` or ``weighted`` policies, VPA 450 Mi or KRR
500 Mi replaced a native 600 Mi increase. Since B.3 the native value decides;
the floor still raises a native representative that chose less (a decrease of
the same engine family) and is recorded in every case.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers import MemoryRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.signals.consensus import ballots_for_signal, consolidate
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    ResourceSpec,
    SourceSignal,
    TargetRef,
)
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from tests.core.optimization.test_oom_rightsizing import (  # pyrefly: ignore[missing-import]
    _snapshot,
    _stats,
    _summary,
)
from tests.core.optimization.test_signal_floors_maturity import (  # pyrefly: ignore[missing-import]
    FakeConnector,
    _engine,
    _krr,
    _memory,
)

MIB = 1024 * 1024


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "oom_floor.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


def _native(proposed: int, *, oom: bool):
    rec = _memory(proposed, current=400 * MIB)
    return rec.model_copy(update={"floor_reason": "oom_kills"}) if oom else rec


def _sources(*, oom: bool):
    return [
        FakeConnector("greenkube", [_native(600 * MIB, oom=oom)]),
        FakeConnector("vpa", [_memory(450 * MIB, "vpa", current=400 * MIB)]),
        _krr(("seaweedfs", "StatefulSet", "seaweedfs-volume", "volume", 400 * MIB, 500 * MIB)),
    ]


class TestOomFloor:
    async def test_an_oom_driven_native_increase_is_never_lowered(self, repo):
        engine = _engine(_sources(oom=True))

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_memory_request_bytes == 600 * MIB
        floor = (record.consensus or {}).get("floor")
        assert floor is not None
        assert floor["reason"] == "oom_kills"
        assert floor["source_id"] == "greenkube"
        assert floor["value"] == 600 * MIB
        # The native value already is 600 Mi: the floor is recorded, but it changed nothing.
        assert floor["applied"] is False
        assert record.consensus["value"] == 600 * MIB

    async def test_a_non_oom_native_increase_is_still_the_native_value(self, repo):
        engine = _engine(_sources(oom=False))

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_memory_request_bytes == 600 * MIB
        assert (record.consensus or {}).get("floor") is None

    async def test_the_floor_is_stored_in_the_consensus_evidence(self, repo, monkeypatch):
        from greenkube.core.optimization import enrich
        from greenkube.models.evidence import RecommendationEvidence

        # The scan has no metric series here: give every recommendation an evidence block to fill.
        monkeypatch.setattr(enrich, "build_evidence", lambda rec, context: RecommendationEvidence())
        engine = _engine(_sources(oom=True))

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.evidence is not None and record.evidence.consensus is not None
        floor = record.evidence.consensus["floor"]
        assert floor == {
            "source_id": "greenkube",
            "signal_id": floor["signal_id"],
            "value": 600 * MIB,
            "reason": "oom_kills",
            "policy_value": 600 * MIB,
            "applied": False,
        }
        assert record.consensus["floor"] == floor
        # The chosen source is the native analyzer, so the record keeps its OOM explanation.
        assert record.source == "greenkube"


def _signal(source_id: str, proposed: int) -> SourceSignal:
    return SourceSignal(
        source_id=source_id,
        target=TargetRef.for_kind("c1", "Deployment", "api", namespace="shop", container="web"),
        capability=Capability.CONTAINER_REQUESTS,
        kind="RIGHTSIZING_MEMORY",
        proposal=ContainerResourcesProposal(
            container="web",
            current=ResourceSpec(memory_request_bytes=400 * MIB),
            proposed=ResourceSpec(memory_request_bytes=proposed),
        ),
        algorithm=AlgorithmInfo(name=source_id),
    )


class TestConsolidateFloor:
    def test_the_floor_holds_without_a_vote_screen(self):
        ballots = [
            *ballots_for_signal(_signal("greenkube", 600 * MIB), source="greenkube", floor_reason="oom_kills"),
            *ballots_for_signal(_signal("vpa", 450 * MIB), source="vpa"),
            *ballots_for_signal(_signal("krr", 500 * MIB)),
        ]

        [outcome] = consolidate(ballots)

        assert outcome.consensus.value == 600 * MIB
        assert outcome.chosen.source == "greenkube"
        assert outcome.consensus.chosen_from == ["greenkube"]
        assert outcome.consensus.floor is not None

    def test_an_external_ballot_never_sets_a_floor(self):
        # Only GreenKube's own measurement proves an OOM: a floor marker on an external ballot is ignored.
        ballots = [
            *ballots_for_signal(_signal("greenkube", 450 * MIB), source="greenkube"),
            *ballots_for_signal(_signal("vpa", 600 * MIB), source="vpa", floor_reason="oom_kills"),
            *ballots_for_signal(_signal("krr", 500 * MIB)),
        ]

        [outcome] = consolidate(ballots)

        assert outcome.consensus.value == 450 * MIB
        assert outcome.consensus.floor is None


class TestAnalyzerMarker:
    def test_the_memory_analyzer_marks_an_oom_driven_increase(self):
        context = OptimizationContext(config=Config(), usage=_snapshot(_summary(oom_kill_count=2)))

        (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)

        assert rec.floor_reason == "oom_kills"

    def test_a_non_oom_increase_carries_no_floor(self):
        busy = _summary(memory_request=100 * MIB, memory_limit=None, memory=_stats(95 * MIB, 99 * MIB))
        context = OptimizationContext(config=Config(), usage=_snapshot(busy))

        (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)

        assert (rec.recommended_memory_request_bytes or 0) > 100 * MIB
        assert rec.floor_reason is None


class TestScanPath:
    async def test_the_real_analyzer_floor_survives_the_scan(self):
        """The native analyzer's own recommendation reaches the consensus with its marker."""
        from greenkube.core.config import get_config
        from greenkube.signals.adapters import analyzer_source_class
        from greenkube.signals.sdk import SourceDeps, SourceSettings

        cfg = get_config()
        context = OptimizationContext(config=cfg, usage=_snapshot(_summary(oom_kill_count=2)))
        native = analyzer_source_class(MemoryRightsizingAnalyzer)(
            SourceSettings(), SourceDeps(config=cfg, cluster_uid="c1")
        )
        vpa = _memory(80 * MIB, "vpa", namespace="demo", kind="Deployment", name="oom-demo", container="app")
        vpa = vpa.model_copy(update={"current_memory_request_bytes": 64 * MIB})
        engine = _engine([native, FakeConnector("vpa", [vpa])])
        engine.build_context = AsyncMock(return_value=context)

        [rec] = (await engine.collect(context)).recommendations

        assert rec.recommended_memory_request_bytes == 96 * MIB
        assert (rec.consensus or {})["floor"]["value"] == 96 * MIB
        assert rec.consensus["floor"]["applied"] is False


def _limit_only(source_id: str = "push:evil", capability: Capability = Capability.CONTAINER_REQUESTS) -> SourceSignal:
    """A container-requests signal whose proposal carries no request value (limits only)."""
    from greenkube.signals.model import SignalOrigin, SignalProvenance

    return SourceSignal(
        source_id=source_id,
        target=TargetRef.for_kind("c1", "Deployment", "api", namespace="shop", container="web"),
        capability=capability,
        kind="RIGHTSIZING_MEMORY",
        proposal=ContainerResourcesProposal(
            container="web",
            current=ResourceSpec(memory_limit_bytes=100 * MIB),
            proposed=ResourceSpec(memory_limit_bytes=50 * MIB),
        ),
        algorithm=AlgorithmInfo(name="x"),
        provenance=SignalProvenance(origin=SignalOrigin.PUSH),
    )


def _native_and_vpa_ballots():
    native = _memory(600 * MIB, current=400 * MIB).model_copy(update={"floor_reason": "oom_kills"})
    vpa = _memory(450 * MIB, "vpa", current=400 * MIB)
    return [
        *ballots_for_signal(
            _signal("greenkube", 600 * MIB),
            source="greenkube",
            floor_reason="oom_kills",
            payload=native,
            payload_kind="RIGHTSIZING_MEMORY",
        ),
        *ballots_for_signal(_signal("vpa", 450 * MIB), source="vpa", payload=vpa, payload_kind="RIGHTSIZING_MEMORY"),
    ]


PUSHED_LIMIT_ONLY = {
    "source_id": "push:evil",
    "complete": True,
    "signals": [
        {
            "target": {"kind": "Deployment", "namespace": "shop", "name": "api", "container": "web"},
            "capability": "container_requests",
            "kind": "RIGHTSIZING_MEMORY",
            "proposal": {
                "kind": "container_resources",
                "container": "web",
                "current": {"memory_limit_bytes": 1},
                "proposed": {"memory_limit_bytes": 2},
            },
            "algorithm": {"name": "x"},
        }
    ],
}


class TestNonNumericBallots:
    """Review finding 1: a request-group ballot without a request value disabled the floor."""

    def test_a_ballot_without_a_request_value_is_ignored_and_the_floor_holds(self):
        from greenkube.core.optimization.dedup import materialize
        from greenkube.signals.consensus import VoteScreen

        ballots = [*_native_and_vpa_ballots(), *ballots_for_signal(_limit_only())]
        assert len({ballot.group_key for ballot in ballots}) == 1

        [outcome] = consolidate(ballots, screen=VoteScreen())
        rec = materialize(outcome)

        assert rec is not None and rec.recommended_memory_request_bytes == 600 * MIB
        assert outcome.consensus.floor is not None
        assert [(vote.source_id, vote.reason) for vote in outcome.consensus.ignored_votes] == [
            ("push:evil", "not_numeric")
        ]

    def test_the_push_endpoint_rejects_a_request_signal_without_a_request_value(self):
        from datetime import datetime, timezone

        from greenkube.signals.connectors.push import parse_batch

        batch, signals, errors = parse_batch(
            PUSHED_LIMIT_ONLY, cluster_uid="c1", received_at=datetime.now(timezone.utc)
        )

        assert batch is None and signals == []
        [error] = errors
        assert error.pointer == "/signals/0/proposal/proposed"
        assert error.type == "missing_resource_value"
        assert "container_requests" in error.message

    def test_the_push_endpoint_rejects_a_limits_signal_without_a_limit_value(self):
        from copy import deepcopy
        from datetime import datetime, timezone

        from greenkube.signals.connectors.push import parse_batch

        document = deepcopy(PUSHED_LIMIT_ONLY)
        document["signals"][0]["capability"] = "container_limits"
        document["signals"][0]["proposal"]["proposed"] = {"memory_request_bytes": 2}
        _batch, _signals, errors = parse_batch(document, cluster_uid="c1", received_at=datetime.now(timezone.utc))

        assert [error.type for error in errors] == ["missing_resource_value"]

    async def test_the_source_runner_drops_a_plugin_request_signal_without_a_request_value(self):
        from datetime import datetime, timezone
        from typing import ClassVar, FrozenSet

        from greenkube.signals.model import AdvisoryProposal
        from greenkube.signals.runtime import SourceRunner, SourceRunStatus
        from greenkube.signals.sdk import CollectContext, Source, SourceBatch, SourceDeps, SourceSettings

        def advisory(kind: str) -> SourceSignal:
            return _signal("acme.sizer", 1).model_copy(
                update={"kind": kind, "proposal": AdvisoryProposal(text="increase its requests")}
            )

        class Sizer(Source):
            id: ClassVar[str] = "acme.sizer"
            display_name: ClassVar[str] = "Acme"
            capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})

            async def collect(self, ctx):
                return SourceBatch(
                    signals=[
                        _limit_only("acme.sizer"),
                        advisory("RIGHTSIZING_MEMORY"),
                        # Advice of its own kind (WORKLOAD_UNDERPROVISIONED) never joins a value group.
                        advisory("WORKLOAD_UNDERPROVISIONED"),
                        _signal("acme.sizer", 300 * MIB),
                    ]
                )

        source = Sizer(SourceSettings(), SourceDeps(config=Config(), cluster_uid="c1"))
        source.registry_origin = "plugin"
        ctx = CollectContext(cluster_uid="c1", namespace=None, window=None, deadline=datetime.now(timezone.utc))

        report = await SourceRunner().run(source, ctx, None)

        assert sorted(signal.kind for signal in report.signals) == ["RIGHTSIZING_MEMORY", "WORKLOAD_UNDERPROVISIONED"]
        [kept] = [signal for signal in report.signals if signal.kind == "RIGHTSIZING_MEMORY"]
        assert getattr(kept.proposal, "proposed").memory_request_bytes == 300 * MIB
        assert report.run.status == SourceRunStatus.INCOMPLETE
        [dropped] = [d for d in report.run.diagnostics if d.code == "missing_resource_value"]
        assert dropped.count == 2

    def test_a_group_of_advice_only_keeps_its_behaviour(self):
        """A request group without any numeric ballot (legacy v1 advice) is not emptied."""
        from greenkube.signals.model import AdvisoryProposal

        advice = _signal("greenkube", 1).model_copy(update={"proposal": AdvisoryProposal(text="check it")})

        [outcome] = consolidate(ballots_for_signal(advice, source="greenkube"))

        assert outcome.consensus.ignored_votes == []


class TestFloorFollowUps:
    def test_hysteresis_never_holds_a_stored_value_below_the_floor(self):
        """Review finding 2: stored 580 Mi stayed in place of a new 600 Mi OOM floor (within the 5 % band)."""
        from greenkube.core.optimization.dedup import apply_revision_hysteresis
        from greenkube.models.metrics import RecommendationRecord

        rec = _memory(600 * MIB, current=400 * MIB).model_copy(update={"floor_reason": "oom_kills"})
        stored = RecommendationRecord.from_recommendation(_memory(580 * MIB, current=400 * MIB))

        [out] = apply_revision_hysteresis([rec], [stored])

        assert out.recommended_memory_request_bytes == 600 * MIB

    def test_hysteresis_still_holds_a_small_non_oom_increase(self):
        from greenkube.core.optimization.dedup import apply_revision_hysteresis
        from greenkube.models.metrics import RecommendationRecord

        rec = _memory(600 * MIB, current=400 * MIB)
        stored = RecommendationRecord.from_recommendation(_memory(580 * MIB, current=400 * MIB))

        [out] = apply_revision_hysteresis([rec], [stored])

        assert out.recommended_memory_request_bytes == 580 * MIB

    def test_an_external_ballot_tied_with_the_floor_never_carries_the_record(self):
        """Review finding 3: on an exact tie an external record was used."""
        ballots = [
            *ballots_for_signal(
                _signal("greenkube", 600 * MIB), source="greenkube", floor_reason="oom_kills", payload="NATIVE"
            ),
            *ballots_for_signal(_signal("vpa", 600 * MIB), source="vpa", payload="VPA"),
            *ballots_for_signal(_signal("krr", 450 * MIB)),
            *ballots_for_signal(_signal("goldi", 440 * MIB)),
            *ballots_for_signal(_signal("other", 430 * MIB)),
        ]

        [outcome] = consolidate(ballots)

        assert outcome.consensus.floor is not None
        assert outcome.chosen.source == "greenkube" and outcome.chosen.payload == "NATIVE"
        assert set(outcome.consensus.chosen_from) == {"greenkube", "vpa"}

    async def test_the_reason_is_unchanged_when_the_floor_did_not_apply(self):
        from greenkube.core.config import get_config

        engine = _engine(_sources(oom=True))
        context = OptimizationContext(config=get_config(), metrics=[])

        [rec] = (await engine.collect(context)).recommendations

        assert rec.consensus is not None and rec.consensus["floor"]["applied"] is False
        assert "floor" not in rec.reason


class TestFloorRaisesTheValue:
    """The floor is not only recorded: it raises a consensus that chose less (review of B.3).

    Two native ballots of one engine family (``greenkube``) are one vote: the
    family's representative is the one with a v1 recommendation, here a 300 Mi
    decrease. The OOM-driven 600 Mi increase of the same family has no payload,
    so only the floor brings the value back to 600 Mi.
    """

    @staticmethod
    def _ballots():
        decrease = _memory(300 * MIB, current=400 * MIB)
        return [
            *ballots_for_signal(
                _signal("greenkube", 300 * MIB),
                source="greenkube",
                payload=decrease,
                payload_kind="RIGHTSIZING_MEMORY",
            ),
            *ballots_for_signal(_signal("greenkube", 600 * MIB), source="greenkube", floor_reason="oom_kills"),
            *ballots_for_signal(_signal("vpa", 450 * MIB), source="vpa"),
        ]

    @pytest.mark.parametrize("screened", [False, True])
    def test_the_oom_floor_raises_the_native_representative(self, screened):
        from greenkube.core.optimization.dedup import materialize
        from greenkube.signals.consensus import VoteScreen

        [outcome] = consolidate(self._ballots(), screen=VoteScreen() if screened else None)

        floor = outcome.consensus.floor
        assert floor is not None and floor.applied is True
        assert floor.policy_value == 300 * MIB
        assert outcome.consensus.value == 600 * MIB
        assert outcome.chosen.floor_reason == "oom_kills"
        rec = materialize(outcome)
        assert rec is not None and rec.recommended_memory_request_bytes == 600 * MIB
        assert "oom_kills floor" in (rec.reason or "")
