"""A native OOM-driven memory increase is a lower bound of every consensus policy.

The OOM kill proves the container needs more than its limit: no external
ballot (VPA, KRR, a pushed or plugin signal) may bring the consolidated value
below GreenKube's OOM-driven increase, whatever ``RECOMMENDATION_CONSENSUS_POLICY``
says. Before the fix the analyzer's ``oom_kills`` trigger only reached the
recommendation as reason text: under ``priority`` (``vpa`` first), ``median`` or
``weighted`` with three voters, VPA 450 Mi or KRR 500 Mi replaced a native
600 Mi increase.
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
POLICIES = ["safest", "priority", "median", "weighted"]


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
    @pytest.mark.parametrize("policy", POLICIES)
    async def test_an_oom_driven_native_increase_is_never_lowered(self, repo, monkeypatch, policy):
        engine = _engine(_sources(oom=True))
        monkeypatch.setattr(engine.config, "RECOMMENDATION_CONSENSUS_POLICY", policy)

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_memory_request_bytes == 600 * MIB
        floor = (record.consensus or {}).get("floor")
        assert floor is not None, policy
        assert floor["reason"] == "oom_kills"
        assert floor["source_id"] == "greenkube"
        assert floor["value"] == 600 * MIB
        # safest already picks 600 Mi: the floor is recorded, but it changed nothing.
        assert floor["applied"] is (policy != "safest")
        assert record.consensus["value"] == 600 * MIB

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [("safest", 600), ("priority", 450), ("median", 500), ("weighted", 500)],
    )
    async def test_a_non_oom_native_increase_keeps_the_policy_behaviour(self, repo, monkeypatch, policy, expected):
        engine = _engine(_sources(oom=False))
        monkeypatch.setattr(engine.config, "RECOMMENDATION_CONSENSUS_POLICY", policy)

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_memory_request_bytes == expected * MIB
        assert (record.consensus or {}).get("floor") is None

    async def test_the_floor_is_stored_in_the_consensus_evidence(self, repo, monkeypatch):
        from greenkube.core.optimization import enrich
        from greenkube.models.evidence import RecommendationEvidence

        # The scan has no metric series here: give every recommendation an evidence block to fill.
        monkeypatch.setattr(enrich, "build_evidence", lambda rec, context: RecommendationEvidence())
        engine = _engine(_sources(oom=True))
        monkeypatch.setattr(engine.config, "RECOMMENDATION_CONSENSUS_POLICY", "priority")

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.evidence is not None and record.evidence.consensus is not None
        floor = record.evidence.consensus["floor"]
        assert floor == {
            "source_id": "greenkube",
            "signal_id": floor["signal_id"],
            "value": 600 * MIB,
            "reason": "oom_kills",
            "policy_value": 450 * MIB,
            "applied": True,
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
    @pytest.mark.parametrize("policy", POLICIES)
    def test_the_floor_holds_without_a_vote_screen(self, policy):
        ballots = [
            *ballots_for_signal(_signal("greenkube", 600 * MIB), source="greenkube", floor_reason="oom_kills"),
            *ballots_for_signal(_signal("vpa", 450 * MIB), source="vpa"),
            *ballots_for_signal(_signal("krr", 500 * MIB)),
        ]

        [outcome] = consolidate(ballots, policy, priorities={"vpa": 30, "krr": 20, "greenkube": 10})

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

        [outcome] = consolidate(ballots, "median")

        assert outcome.consensus.value == 500 * MIB
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
    async def test_the_real_analyzer_floor_survives_the_scan_under_priority(self, monkeypatch):
        """The native analyzer's own recommendation reaches the consensus with its marker."""
        from greenkube.core.config import get_config
        from greenkube.signals.adapters import analyzer_source_class
        from greenkube.signals.sdk import SourceDeps, SourceSettings

        cfg = get_config()
        monkeypatch.setattr(cfg, "RECOMMENDATION_CONSENSUS_POLICY", "priority")
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
        assert (rec.consensus or {})["floor"]["applied"] is True
        assert rec.consensus["floor"]["policy_value"] == 80 * MIB
