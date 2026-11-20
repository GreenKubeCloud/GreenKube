"""Tool floors, VPA maturity and source-only advice (roadmap A.5, field report F-12).

The field scenarios of the report are pinned here:

* seaweedfs-volume: GreenKube measured 42 Mi, VPA (Fairwinds chart floor) and KRR
  (``simple`` floor) both proposed 100 Mi; ``safest`` kept 100 Mi. A value equal to
  a known tool minimum is not a signal of need: the native value must win.
* csi-nfs-controller ``liveness-probe`` sidecar: KRR alone (no native proposal)
  raised 20 Mi to 100 Mi with ``agreement: 1.0``. A source alone never creates a
  recommendation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, FrozenSet, List
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.metrics import RecommendationSource as SourceName
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    ResourceSpec,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
)
from greenkube.signals.sdk import Source, SourceBatch, SourceDeps, SourceSettings
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

MIB = 1024 * 1024
#: GreenKube's measured need of seaweedfs-volume (42 Mi) plus 15 % headroom.
NATIVE_SEAWEEDFS = int(42 * MIB * 1.15)


class FakeConnector(RecommendationSource):
    def __init__(self, name: str, recs: List[Recommendation]):
        self.name = name
        self._recs = recs

    async def is_available(self) -> bool:
        return True

    async def collect(self, context):
        return list(self._recs)


def _memory(
    proposed: int,
    source: str = "greenkube",
    *,
    namespace: str = "seaweedfs",
    kind: str = "StatefulSet",
    name: str = "seaweedfs-volume",
    container: str = "volume",
    current: int = 1024 * MIB,
) -> Recommendation:
    return Recommendation(
        pod_name=name,
        container_name=container,
        namespace=namespace,
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind=kind,
        owner_name=name,
        description=f"{source} proposes {proposed}",
        current_memory_request_bytes=current,
        recommended_memory_request_bytes=proposed,
        source=SourceName(source),
    )


def _cpu(proposed: int, source: str = "greenkube", current: int = 500) -> Recommendation:
    return Recommendation(
        pod_name="api",
        container_name="web",
        namespace="shop",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description=f"{source} proposes {proposed}m",
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=proposed,
        source=SourceName(source),
    )


class PushedKrr(Source):
    """KRR report signals replayed from the inbox (origin push), memory only."""

    id: ClassVar[str] = "krr"
    display_name: ClassVar[str] = "KRR"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    targets: ClassVar[list] = []

    async def collect(self, ctx):
        signals = []
        for namespace, kind, name, container, current, proposed in type(self).targets:
            signals.append(
                SourceSignal(
                    source_id="krr",
                    target=TargetRef.for_kind(ctx.cluster_uid, kind, name, namespace=namespace, container=container),
                    capability=Capability.CONTAINER_REQUESTS,
                    kind="RIGHTSIZING_MEMORY",
                    proposal=ContainerResourcesProposal(
                        container=container,
                        current=ResourceSpec(memory_request_bytes=current),
                        proposed=ResourceSpec(memory_request_bytes=proposed),
                    ),
                    algorithm=AlgorithmInfo(name="krr.simple"),
                    provenance=SignalProvenance(origin=SignalOrigin.PUSH),
                )
            )
        return SourceBatch(signals=signals)


def _krr(*targets) -> Source:
    source_class = type("Krr", (PushedKrr,), {"targets": list(targets)})
    return source_class(SourceSettings(), SourceDeps(config=get_config(), cluster_uid="c1"))


def _engine(sources) -> OptimizationEngine:
    engine = OptimizationEngine(sources=sources)
    engine.build_context = AsyncMock(return_value=OptimizationContext(config=get_config(), metrics=[]))
    return engine


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "floors.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


class TestFieldScenarios:
    async def test_seaweedfs_volume_keeps_the_native_value_over_tool_floors(self, repo):
        engine = _engine(
            [
                FakeConnector("greenkube", [_memory(NATIVE_SEAWEEDFS)]),
                FakeConnector("vpa", [_memory(100 * MIB, "vpa")]),
                _krr(("seaweedfs", "StatefulSet", "seaweedfs-volume", "volume", 1024 * MIB, 100 * MIB)),
            ]
        )

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_memory_request_bytes == NATIVE_SEAWEEDFS
        consensus = record.consensus or {}
        assert consensus["contested"] is False
        ignored = {vote["source_id"]: vote["reason"] for vote in consensus["ignored_votes"]}
        assert ignored == {"vpa": "tool_floor", "krr": "tool_floor"}

    async def test_csi_nfs_sidecar_gets_no_recommendation_from_krr_alone(self, repo):
        engine = _engine(
            [_krr(("kube-system", "Deployment", "csi-nfs-controller", "liveness-probe", 20 * MIB, 100 * MIB))]
        )

        persisted = await engine.refresh(AsyncMock(), AsyncMock(), repo)

        assert persisted == []
        assert await repo.get_active_recommendations() == []

    async def test_krr_alone_below_its_floor_still_creates_nothing(self, repo):
        # A real (non-floor) KRR proposal with no native measurement stays a signal too.
        engine = _engine([_krr(("shop", "Deployment", "api", "web", 512 * MIB, 300 * MIB))])

        assert await engine.refresh(AsyncMock(), AsyncMock(), repo) == []

    async def test_vpa_alone_creates_no_recommendation(self, repo):
        engine = _engine([FakeConnector("vpa", [_cpu(300, "vpa")])])

        assert await engine.refresh(AsyncMock(), AsyncMock(), repo) == []

    async def test_an_insignificant_vpa_change_is_not_a_vote(self, repo):
        # VPA 490m on a 500m request is a 2 % change: below 5 % it is not a vote.
        engine = _engine([FakeConnector("greenkube", [_cpu(400)]), FakeConnector("vpa", [_cpu(490, "vpa")])])

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_cpu_request_millicores == 400
        ignored = {vote["source_id"]: vote["reason"] for vote in (record.consensus or {})["ignored_votes"]}
        assert ignored == {"vpa": "below_min_change"}

    async def test_a_measured_external_vote_above_the_floor_still_counts(self, repo):
        engine = _engine(
            [
                FakeConnector("greenkube", [_memory(NATIVE_SEAWEEDFS)]),
                _krr(("seaweedfs", "StatefulSet", "seaweedfs-volume", "volume", 1024 * MIB, 128 * MIB)),
            ]
        )

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        # safest: KRR's measured 128 Mi is a real signal and is the safer value.
        assert record.recommended_memory_request_bytes == 128 * MIB
        assert (record.consensus or {}).get("ignored_votes") == []

    async def test_floors_are_configurable_per_source(self, repo, monkeypatch):
        engine = _engine(
            [
                FakeConnector("greenkube", [_memory(NATIVE_SEAWEEDFS)]),
                _krr(("seaweedfs", "StatefulSet", "seaweedfs-volume", "volume", 1024 * MIB, 100 * MIB)),
            ]
        )
        # This KRR runs with --mem-min 64: 100 Mi is a measured value, not its floor.
        monkeypatch.setattr(engine.config, "SIGNAL_TOOL_FLOORS", '{"krr": {"memory": "64Mi"}}')

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_memory_request_bytes == 100 * MIB


class TestVpaMaturity:
    @staticmethod
    def _vpa(**maturity):
        from greenkube.collectors.vpa_collector import VPARecommendation

        return VPARecommendation(
            namespace="shop",
            vpa_name="api",
            target_kind="Deployment",
            target_name="api",
            container_name="web",
            cpu_target_millicores=200,
            memory_target_bytes=200 * MIB,
            **maturity,
        )

    @staticmethod
    async def _collect(vpas, monkeypatch=None, hours=None):
        from greenkube.core.optimization.providers.vpa import VpaSource
        from greenkube.models.metrics import CombinedMetric

        cfg = get_config()
        if hours is not None and monkeypatch is not None:
            monkeypatch.setattr(cfg, "VPA_MIN_SAMPLE_HOURS", hours)

        class Collector:
            async def collect(self):
                return list(vpas)

        source = VpaSource(cfg, collector=Collector())
        now = datetime.now(timezone.utc)
        metric = CombinedMetric(
            pod_name="api-1",
            namespace="shop",
            container_name="web",
            owner_kind="Deployment",
            owner_name="api",
            cpu_request=500,
            memory_request=512 * MIB,
            timestamp=now,
        )
        context = OptimizationContext(config=cfg, metrics=[metric])
        return source, await source.collect(context)

    async def test_a_vpa_with_minutes_of_samples_is_not_a_vote(self):
        start = datetime.now(timezone.utc) - timedelta(minutes=28)
        vpa = self._vpa(total_samples=28, first_sample_start=start, last_sample_start=start + timedelta(minutes=27))

        source, recs = await self._collect([vpa])

        assert recs == []
        assert source.immature_vpas == {"shop/api"}

    async def test_a_vpa_with_a_day_of_samples_votes(self):
        start = datetime.now(timezone.utc) - timedelta(hours=30)
        vpa = self._vpa(total_samples=1800, first_sample_start=start, last_sample_start=start + timedelta(hours=30))

        _source, recs = await self._collect([vpa])

        assert {rec.type for rec in recs} == {RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY}

    async def test_sample_count_alone_measures_maturity(self, monkeypatch):
        # Without sample timestamps, totalSamplesCount at one sample a minute: 600 samples = 10 h.
        _source, recs = await self._collect([self._vpa(total_samples=600)], monkeypatch, hours=8)
        assert recs
        _source, recs = await self._collect([self._vpa(total_samples=600)], monkeypatch, hours=24)
        assert recs == []

    async def test_low_confidence_condition_is_not_mature(self):
        start = datetime.now(timezone.utc) - timedelta(hours=30)
        vpa = self._vpa(
            total_samples=1800,
            first_sample_start=start,
            last_sample_start=start + timedelta(hours=30),
            conditions={"LowConfidence": "True", "RecommendationProvided": "True"},
        )

        _source, recs = await self._collect([vpa])

        assert recs == []

    async def test_unknown_maturity_votes_with_a_warning(self):
        from greenkube.core.optimization.providers.vpa import VpaSignalSource

        source, recs = await self._collect([self._vpa()])

        assert recs
        assert source.unknown_maturity_vpas == {"shop/api"}
        adapter = VpaSignalSource(SourceSettings(), SourceDeps(config=get_config(), cluster_uid="c1"), connector=source)
        adapter.namespace_lister = AsyncMock(return_value=None)
        codes = {diagnostic.code: diagnostic.level for diagnostic in await adapter._diagnostics()}
        assert codes.get("vpa_maturity_unknown") == "warning"

    async def test_the_minimum_is_configurable(self, monkeypatch):
        start = datetime.now(timezone.utc) - timedelta(minutes=28)
        vpa = self._vpa(total_samples=28, first_sample_start=start, last_sample_start=start + timedelta(minutes=27))

        _source, recs = await self._collect([vpa], monkeypatch, hours=0)

        assert recs


class TestCheckpointCollection:
    async def test_collector_reads_conditions_and_checkpoints(self, monkeypatch):
        from greenkube.collectors import vpa_collector

        vpa_item = {
            "metadata": {"namespace": "shop", "name": "api"},
            "spec": {"targetRef": {"kind": "Deployment", "name": "api"}, "updatePolicy": {"updateMode": "Off"}},
            "status": {
                "conditions": [{"type": "RecommendationProvided", "status": "True"}],
                "recommendation": {
                    "containerRecommendations": [
                        {"containerName": "web", "target": {"cpu": "15m", "memory": "104857600"}}
                    ]
                },
            },
        }
        checkpoint = {
            "metadata": {"namespace": "shop", "name": "api-web"},
            "spec": {"vpaObjectName": "api", "containerName": "web"},
            "status": {
                "totalSamplesCount": 28,
                "firstSampleStart": "2026-11-16T10:00:00Z",
                "lastSampleStart": "2026-11-16T10:27:00Z",
            },
        }

        class Api:
            async def list_cluster_custom_object(self, group, version, plural, **kwargs):
                if plural == vpa_collector.VPA_CHECKPOINT_PLURAL:
                    return {"items": [checkpoint]}
                return {"items": [vpa_item]}

        monkeypatch.setattr(vpa_collector, "get_custom_objects_api", AsyncMock(return_value=Api()))

        [vpa] = await vpa_collector.VPACollector().collect()

        assert vpa.conditions == {"RecommendationProvided": "True"}
        assert vpa.total_samples == 28
        assert vpa.containers == 1
        assert vpa.first_sample_start is not None and vpa.last_sample_start is not None
        assert vpa.last_sample_start - vpa.first_sample_start == timedelta(minutes=27)
