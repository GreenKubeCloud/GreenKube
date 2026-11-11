"""The engine builds one recommendation per target by consensus across sources (roadmap E6, E7, E8)."""

from __future__ import annotations

from typing import ClassVar, FrozenSet, List, Optional
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.metrics import RecommendationSource as SourceName
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    ResourceSpec,
    SourceSignal,
    TargetRef,
)
from greenkube.signals.sdk import Source, SourceBatch, SourceDeps, SourceSettings
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


class FakeConnector(RecommendationSource):
    def __init__(self, name: str, recs: Optional[List[Recommendation]] = None, *, error: Optional[Exception] = None):
        self.name = name
        self._recs = recs or []
        self._error = error

    async def is_available(self) -> bool:
        return True

    async def collect(self, context):
        if self._error:
            raise self._error
        return list(self._recs)


def _cpu(cpu: int, source: str = "greenkube", **overrides) -> Recommendation:
    values = dict(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description=f"{source} proposes {cpu}m",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=cpu,
        potential_savings_cost=float(500 - cpu),
        source=SourceName(source),
    )
    values.update(overrides)
    return Recommendation(**values)


class KrrSource(Source):
    """A signal-only third-party source (no v1 recommendations)."""

    id: ClassVar[str] = "krr"
    display_name: ClassVar[str] = "KRR"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    proposed: ClassVar[int] = 250
    fail: ClassVar[bool] = False

    async def collect(self, ctx):
        if type(self).fail:
            raise RuntimeError("krr job failed")
        signal = SourceSignal(
            source_id="krr",
            target=TargetRef.for_kind(ctx.cluster_uid, "Deployment", "api", namespace="prod"),
            capability=Capability.CONTAINER_REQUESTS,
            kind="RIGHTSIZING_CPU",
            proposal=ContainerResourcesProposal(
                current=ResourceSpec(cpu_request_m=500), proposed=ResourceSpec(cpu_request_m=type(self).proposed)
            ),
            algorithm=AlgorithmInfo(name="krr.simple"),
        )
        return SourceBatch(signals=[signal])


def _krr(proposed: int = 250, fail: bool = False) -> Source:
    source_class = type("Krr", (KrrSource,), {"proposed": proposed, "fail": fail})
    return source_class(SourceSettings(), SourceDeps(config=get_config(), cluster_uid="c1"))


def _engine(sources) -> OptimizationEngine:
    engine = OptimizationEngine(sources=sources)
    engine.build_context = AsyncMock(return_value=OptimizationContext(config=get_config(), metrics=[]))
    return engine


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "consensus.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


async def _only(repo) -> RecommendationRecord:
    [record] = await repo.get_active_recommendations()
    return record


class TestConsensusScan:
    async def test_roadmap_fixture_vpa_krr_native_gives_safest_300m(self, repo):
        engine = _engine([FakeConnector("vpa", [_cpu(300, "vpa")]), _krr(250), FakeConnector("greenkube", [_cpu(280)])])

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        record = await _only(repo)
        assert record.recommended_cpu_request_millicores == 300
        assert record.source == SourceName.VPA
        assert set(record.sources) == {"vpa", "krr", "greenkube"} and record.sources[0] == "vpa"
        consensus = record.consensus or {}
        assert consensus["policy"] == "safest"
        assert consensus["agreement"] == pytest.approx(0.83, abs=0.005)
        assert consensus["contested"] is False
        assert consensus["spread"]["cpu_request_m"] == {"vpa": 300, "krr": 250, "greenkube": 280}
        # The v2 identity, target and proposal are persisted with the record.
        assert record.fingerprint_v4 and len(record.fingerprint_v4) == 64
        assert (record.target or {})["kind"] == "Deployment"
        assert (record.proposal or {})["proposed"]["cpu_request_m"] == 300
        assert record.revision == 1
        # The projection is the chosen source's own (VPA proposed 300m).
        assert record.potential_savings_cost == 200.0

    async def test_consensus_reaches_the_evidence_and_rewards_agreement(self):
        engine = _engine([FakeConnector("vpa", [_cpu(300, "vpa")]), FakeConnector("greenkube", [_cpu(290)])])
        alone = _engine([FakeConnector("vpa", [_cpu(300, "vpa")])])
        context = OptimizationContext(config=get_config(), metrics=[])

        [agreed] = (await engine.collect(context)).recommendations
        [single] = (await alone.collect(context)).recommendations

        assert agreed.consensus is not None and agreed.consensus["independent_votes"] == 2
        if agreed.evidence is not None:
            assert agreed.evidence.consensus == agreed.consensus
        if single.confidence is not None and agreed.confidence is not None and single.confidence < 1:
            assert agreed.confidence > single.confidence

    async def test_a_signal_only_source_can_carry_the_consensus(self, repo):
        engine = _engine([_krr(350), FakeConnector("greenkube", [_cpu(280)])])

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        record = await _only(repo)
        assert record.recommended_cpu_request_millicores == 350
        assert record.source == "krr"
        # KRR's own savings claim is never copied: the value is projected again (or left unknown).
        assert record.potential_savings_cost != 220.0
        assert "Consensus across sources" in record.reason

    async def test_priority_policy_keeps_the_former_arbitration(self, repo, monkeypatch):
        engine = _engine([FakeConnector("vpa", [_cpu(200, "vpa")]), FakeConnector("greenkube", [_cpu(280)])])
        monkeypatch.setattr(engine.config, "RECOMMENDATION_CONSENSUS_POLICY", "priority")

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        record = await _only(repo)
        assert (record.source, record.recommended_cpu_request_millicores) == (SourceName.VPA, 200)


class TestRevisionsAcrossScans:
    async def test_a_new_consensus_value_is_a_new_revision(self, repo):
        await _engine([FakeConnector("vpa", [_cpu(300, "vpa")]), _krr(250)]).refresh(AsyncMock(), AsyncMock(), repo)
        first = await _only(repo)

        await _engine([FakeConnector("vpa", [_cpu(300, "vpa")]), _krr(250)]).refresh(AsyncMock(), AsyncMock(), repo)
        assert (await _only(repo)).revision == 1

        await _engine([FakeConnector("vpa", [_cpu(320, "vpa")]), _krr(250)]).refresh(AsyncMock(), AsyncMock(), repo)
        second = await _only(repo)
        assert (second.id, second.revision, second.recommended_cpu_request_millicores) == (first.id, 2, 320)
        [superseded] = await repo.get_revisions(first.id)
        assert (superseded.revision, superseded.snapshot["recommended_cpu_request_millicores"]) == (1, 300)
        assert (superseded.consensus or {})["value"] == 300


class TestPerSourceReconciliation:
    """E8 with consensus: staleness needs every supporting source to complete without re-emitting."""

    async def test_a_failed_supporting_source_keeps_the_last_known_consensus(self, repo):
        await _engine([FakeConnector("vpa", [_cpu(300, "vpa")]), _krr(250)]).refresh(AsyncMock(), AsyncMock(), repo)

        # VPA is down: KRR alone (250m) must not overwrite the 300m consensus VPA supported.
        engine = _engine([FakeConnector("vpa", error=RuntimeError("apiserver timeout")), _krr(250)])
        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        record = await _only(repo)
        assert record.recommended_cpu_request_millicores == 300
        assert record.revision == 1

    async def test_a_failed_signal_only_source_never_makes_its_advice_stale(self, repo):
        await _engine([_krr(250)]).refresh(AsyncMock(), AsyncMock(), repo)

        await _engine([_krr(fail=True), FakeConnector("greenkube", [])]).refresh(AsyncMock(), AsyncMock(), repo)

        record = await _only(repo)
        assert record.status is RecommendationStatus.ACTIVE and record.source == "krr"

    async def test_stale_only_when_every_supporting_source_completed_without_it(self, repo):
        await _engine([FakeConnector("vpa", [_cpu(300, "vpa")]), _krr(250)]).refresh(AsyncMock(), AsyncMock(), repo)

        # VPA completes without it, KRR fails: KRR still supports it, so it stays.
        await _engine([FakeConnector("vpa", []), _krr(fail=True)]).refresh(AsyncMock(), AsyncMock(), repo)
        assert len(await repo.get_active_recommendations()) == 1

        # Both complete and neither re-emits it: stale.
        await _engine([FakeConnector("vpa", []), _empty_krr()]).refresh(AsyncMock(), AsyncMock(), repo)
        assert await repo.get_active_recommendations() == []


def _empty_krr() -> Source:
    class EmptyKrr(KrrSource):
        async def collect(self, ctx):
            return SourceBatch(signals=[])

    return EmptyKrr(SourceSettings(), SourceDeps(config=get_config(), cluster_uid="c1"))
