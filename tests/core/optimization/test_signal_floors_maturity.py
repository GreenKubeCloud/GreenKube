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

    async def test_an_insignificant_vpa_change_is_a_keep_vote(self, repo):
        # VPA 490m on a 500m request is a 2 % change: below 5 % it votes "keep 500m", like VPA 500m would.
        engine = _engine([FakeConnector("greenkube", [_cpu(400)]), FakeConnector("vpa", [_cpu(490, "vpa")])])

        assert await engine.refresh(AsyncMock(), AsyncMock(), repo) == []

    async def test_a_small_change_holds_the_value_like_a_keep_vote(self, repo):
        # Current 400 Mi, native 200 Mi: VPA 400 Mi holds 400 Mi under safest, so VPA 410 Mi must too.
        for vpa_value in (400 * MIB, 410 * MIB):
            engine = _engine(
                [
                    FakeConnector("greenkube", [_memory(200 * MIB, current=400 * MIB)]),
                    FakeConnector("vpa", [_memory(vpa_value, "vpa", current=400 * MIB)]),
                ]
            )

            assert await engine.refresh(AsyncMock(), AsyncMock(), repo) == [], vpa_value

    @pytest.mark.parametrize("policy", ["safest", "priority", "median", "weighted"])
    async def test_a_small_change_never_holds_back_a_native_increase(self, repo, monkeypatch, policy):
        # Current 400 Mi, native 600 Mi (an increase, e.g. OOM-driven), VPA 410 Mi (below the minimum)
        # and KRR 380 Mi: "keep 400 Mi" must not win and cancel the increase.
        engine = _engine(
            [
                FakeConnector("greenkube", [_memory(600 * MIB, current=400 * MIB)]),
                FakeConnector("vpa", [_memory(410 * MIB, "vpa", current=400 * MIB)]),
                _krr(("seaweedfs", "StatefulSet", "seaweedfs-volume", "volume", 400 * MIB, 380 * MIB)),
            ]
        )
        monkeypatch.setattr(engine.config, "RECOMMENDATION_CONSENSUS_POLICY", policy)

        await engine.refresh(AsyncMock(), AsyncMock(), repo)

        [record] = await repo.get_active_recommendations()
        assert record.recommended_memory_request_bytes == 600 * MIB
        ignored = {vote["source_id"]: vote["reason"] for vote in (record.consensus or {})["ignored_votes"]}
        assert ignored == {"vpa": "below_min_change"}

    def test_a_small_change_stays_listed_as_ignored(self):
        from greenkube.signals.consensus import VoteScreen, ballots_for_signal, consolidate

        def signal(source_id: str, proposed: int) -> SourceSignal:
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

        ballots = [
            *ballots_for_signal(signal("greenkube", 200 * MIB), source="greenkube"),
            *ballots_for_signal(signal("vpa", 410 * MIB)),
        ]

        [outcome] = consolidate(ballots, "safest", screen=VoteScreen())

        consensus = outcome.consensus
        assert [(vote.source_id, vote.reason) for vote in consensus.ignored_votes] == [("vpa", "below_min_change")]
        # The VPA vote counts at the current request (400 Mi), not at 410 Mi, and holds it.
        assert consensus.spread["memory_request_bytes"]["vpa"] == 400 * MIB
        assert consensus.value == 400 * MIB

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

    @pytest.mark.parametrize("policy", ["priority", "median", "weighted"])
    async def test_unknown_maturity_never_pushes_a_decrease(self, policy, monkeypatch):
        from greenkube.core.optimization.providers.vpa import VpaSignalSource, VpaSource
        from greenkube.models.metrics import CombinedMetric

        cfg = get_config()
        monkeypatch.setattr(cfg, "RECOMMENDATION_CONSENSUS_POLICY", policy)
        # No checkpoint: maturity unknown. VPA proposes 50m where GreenKube measured a need of 400m.
        vpa = self._vpa()
        vpa.cpu_target_millicores = 50
        vpa.memory_target_bytes = None

        class Collector:
            async def collect(self):
                return [vpa]

        source = VpaSignalSource(
            SourceSettings(),
            SourceDeps(config=cfg, cluster_uid="c1"),
            connector=VpaSource(cfg, collector=Collector()),
            namespace_lister=AsyncMock(return_value=None),
        )
        metric = CombinedMetric(
            pod_name="api-1",
            namespace="shop",
            container_name="web",
            owner_kind="Deployment",
            owner_name="api",
            cpu_request=500,
            memory_request=512 * MIB,
            timestamp=datetime.now(timezone.utc),
        )
        context = OptimizationContext(config=cfg, metrics=[metric])
        engine = OptimizationEngine(config=cfg, sources=[source, FakeConnector("greenkube", [_cpu(400)])])

        [rec] = (await engine.collect(context)).recommendations

        assert rec.recommended_cpu_request_millicores == 400
        ignored = {vote["source_id"]: vote["reason"] for vote in (rec.consensus or {})["ignored_votes"]}
        assert ignored == {"vpa": "maturity_unknown"}


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

    @staticmethod
    def _api(monkeypatch, checkpoints, created=None):
        from greenkube.collectors import vpa_collector

        metadata = {"namespace": "shop", "name": "api"}
        if created is not None:
            metadata["creationTimestamp"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")
        vpa_item = {
            "metadata": metadata,
            "spec": {"targetRef": {"kind": "Deployment", "name": "api"}, "updatePolicy": {"updateMode": "Off"}},
            "status": {"recommendation": {"containerRecommendations": [{"containerName": "web", "target": {}}]}},
        }

        class Api:
            async def list_cluster_custom_object(self, group, version, plural, **kwargs):
                if plural == vpa_collector.VPA_CHECKPOINT_PLURAL:
                    if isinstance(checkpoints, Exception):
                        raise checkpoints
                    return {"items": checkpoints}
                return {"items": [vpa_item]}

        monkeypatch.setattr(vpa_collector, "get_custom_objects_api", AsyncMock(return_value=Api()))
        return vpa_collector.VPACollector()

    async def test_a_container_without_a_checkpoint_next_to_checkpointed_ones_has_no_samples(self, monkeypatch):
        sidecar = {
            "metadata": {"namespace": "shop"},
            "spec": {"vpaObjectName": "api", "containerName": "sidecar"},
            "status": {"totalSamplesCount": 3000},
        }
        collector = self._api(monkeypatch, [sidecar])

        [vpa] = await collector.collect()

        assert collector.checkpoints_readable is True
        assert vpa.total_samples == 0

    async def test_a_new_vpa_without_a_checkpoint_has_no_samples(self, monkeypatch):
        collector = self._api(monkeypatch, [], created=datetime.now(timezone.utc) - timedelta(minutes=20))

        [vpa] = await collector.collect()

        assert vpa.total_samples == 0

    async def test_no_checkpoint_at_all_leaves_maturity_unknown(self, monkeypatch):
        # The recommender in --storage=prometheus mode writes no checkpoints: never set every VPA aside.
        collector = self._api(monkeypatch, [], created=datetime.now(timezone.utc) - timedelta(days=30))

        [vpa] = await collector.collect()

        assert vpa.total_samples is None

    async def test_an_old_vpa_without_its_own_checkpoint_in_an_empty_list_is_unknown(self, monkeypatch):
        collector = self._api(monkeypatch, [])

        [vpa] = await collector.collect()

        assert vpa.total_samples is None

    async def test_unreadable_checkpoints_leave_maturity_unknown(self, monkeypatch):
        collector = self._api(monkeypatch, PermissionError("forbidden"))

        [vpa] = await collector.collect()

        assert collector.checkpoints_readable is False
        assert vpa.total_samples is None


class TestPluginsAreNeverNative:
    async def test_a_plugin_declaring_a_greenkube_engine_family_is_not_a_native_vote(self, repo):
        class Impostor(Source):
            id: ClassVar[str] = "acme.sizer"
            display_name: ClassVar[str] = "Acme"
            capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})

            async def collect(self, ctx):
                return SourceBatch(
                    signals=[
                        SourceSignal(
                            source_id="acme.sizer",
                            engine_family="greenkube.hygiene",
                            target=TargetRef.for_kind(
                                ctx.cluster_uid, "Deployment", "api", namespace="shop", container="web"
                            ),
                            capability=Capability.CONTAINER_REQUESTS,
                            kind="RIGHTSIZING_CPU",
                            proposal=ContainerResourcesProposal(
                                container="web",
                                current=ResourceSpec(cpu_request_m=500),
                                proposed=ResourceSpec(cpu_request_m=100),
                            ),
                            algorithm=AlgorithmInfo(name="acme"),
                        )
                    ]
                )

        plugin = Impostor(SourceSettings(), SourceDeps(config=get_config(), cluster_uid="c1"))
        plugin.registry_origin = "plugin"

        assert await _engine([plugin]).refresh(AsyncMock(), AsyncMock(), repo) == []


class TestToolFloorSettings:
    def test_defaults_are_the_krr_simple_and_fairwinds_vpa_floors(self):
        from greenkube.core.config import parse_tool_floors

        assert parse_tool_floors(Config().SIGNAL_TOOL_FLOORS) == {
            "krr": {"cpu_request_m": 10, "memory_request_bytes": 100 * MIB},
            "vpa": {"cpu_request_m": 15, "memory_request_bytes": 100 * MIB},
        }

    def test_an_empty_object_disables_floors(self):
        from greenkube.core.config import parse_tool_floors

        assert parse_tool_floors("{}") == {}

    @pytest.mark.parametrize("value", ["not json", "[]", '{"krr": {"disk": "1Gi"}}', '{"krr": {"memory": "lots"}}'])
    def test_invalid_floors_are_rejected_at_startup(self, value):
        with pytest.raises(ValueError):
            Config(SIGNAL_TOOL_FLOORS=value)


class TestPerPodVpaFloor:
    @staticmethod
    def _ballot(value: int, containers=None, source_id: str = "vpa"):
        from greenkube.signals.consensus import ballots_for_signal

        signal = SourceSignal(
            source_id=source_id,
            engine_family="vpa",
            target=TargetRef.for_kind(
                "c1", "Deployment", "csi-nfs-controller", namespace="kube-system", container="nfs"
            ),
            capability=Capability.CONTAINER_REQUESTS,
            kind="RIGHTSIZING_MEMORY",
            proposal=ContainerResourcesProposal(
                container="nfs",
                current=ResourceSpec(memory_request_bytes=20 * MIB),
                proposed=ResourceSpec(memory_request_bytes=value),
            ),
            algorithm=AlgorithmInfo(name="vpa-recommender"),
            raw={"containers": containers} if containers else {},
        )
        [ballot] = ballots_for_signal(signal)
        return ballot

    def test_the_vpa_floor_is_split_between_the_pod_containers(self):
        from greenkube.signals.consensus import VoteScreen

        screen = VoteScreen()
        # Three containers: the recommender's 100 Mi pod minimum gives each 33.3 Mi.
        assert screen.reason(self._ballot(int(100 * MIB / 3), containers=3)) == "tool_floor"
        assert screen.reason(self._ballot(50 * MIB, containers=3)) is None
        assert screen.reason(self._ballot(100 * MIB, containers=3)) is None
        # Unknown container count: only the whole pod floor is recognised (one container).
        assert screen.reason(self._ballot(100 * MIB)) == "tool_floor"
        assert screen.reason(self._ballot(50 * MIB)) is None

    def test_a_small_sidecar_value_is_not_mistaken_for_a_split_floor(self):
        from greenkube.signals.consensus import VoteScreen, ballots_for_signal

        signal = SourceSignal(
            source_id="vpa",
            engine_family="vpa",
            target=TargetRef.for_kind("c1", "Deployment", "csi-nfs-controller", namespace="kube-system", container="p"),
            capability=Capability.CONTAINER_REQUESTS,
            kind="RIGHTSIZING_CPU",
            proposal=ContainerResourcesProposal(
                container="p", current=ResourceSpec(cpu_request_m=100), proposed=ResourceSpec(cpu_request_m=8)
            ),
            algorithm=AlgorithmInfo(name="vpa-recommender"),
        )
        [ballot] = ballots_for_signal(signal)

        assert VoteScreen().reason(ballot) is None

    def test_a_goldilocks_floor_overrides_the_vpa_family_floor(self):
        from greenkube.signals.consensus import VoteScreen

        screen = VoteScreen(
            floors={"vpa": {"memory_request_bytes": 100 * MIB}, "goldilocks": {"memory_request_bytes": 64 * MIB}}
        )

        assert screen.reason(self._ballot(100 * MIB, containers=1, source_id="goldilocks")) is None
        assert screen.reason(self._ballot(64 * MIB, containers=1, source_id="goldilocks")) == "tool_floor"

    def test_a_source_floor_without_the_resource_falls_through_to_the_family(self):
        from greenkube.signals.consensus import VoteScreen

        screen = VoteScreen(floors={"vpa": {"memory_request_bytes": 100 * MIB}, "goldilocks": {"cpu_request_m": 25}})

        assert screen.reason(self._ballot(100 * MIB, containers=1, source_id="goldilocks")) == "tool_floor"
