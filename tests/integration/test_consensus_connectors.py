"""Connectors become consolidated recommendations through consensus (roadmap E6 x G1, G2, G3, G6).

Engine-level integration with the real connectors and their recorded fixtures:

* VPA connector over ``goldilocks_vpas.json`` (one Goldilocks VPA at 300m,
  one user VPA at 250m, both 512Mi, on ``shop/Deployment/api/web``);
* KRR report parser over ``krr_report.json`` (250m, 512Mi on the same container);
* push batches parsed by ``parse_batch`` and read by the push source.

Every source runs through the engine's source runner and the consensus
engine; nothing between them is mocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.providers.vpa import VpaSignalSource, VpaSource
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType
from greenkube.signals.connectors.inbox import InboxBatch, InboxSettings
from greenkube.signals.connectors.krr import KrrSource, parse_krr_report
from greenkube.signals.connectors.push import PushSignalSource, parse_batch
from greenkube.signals.consensus import Consensus
from greenkube.signals.sdk import SourceDeps, SourceSettings
from tests.sources.fake_inbox import MemoryInbox  # pyrefly: ignore[missing-import]
from tests.sources.test_goldilocks_provenance import FixtureCollector  # pyrefly: ignore[missing-import]

FIXTURES = Path(__file__).resolve().parents[1] / "sources" / "fixtures"
MIB = 1024 * 1024
CLUSTER = "default"
TARGET = "shop/Deployment/api/web"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _config() -> Config:
    return Config().model_copy(update={"CLUSTER_NAME": CLUSTER})


def _context(config: Config) -> OptimizationContext:
    now = _now()
    metrics = [
        CombinedMetric(
            pod_name=f"api-7d9f8b6c5-{suffix}",
            namespace="shop",
            owner_kind="Deployment",
            owner_name="api",
            container_name="web",
            cpu_request=1000,
            memory_request=1024 * MIB,
            cpu_usage_millicores=150,
            memory_usage_bytes=300 * MIB,
            total_cost=0.05,
            co2e_grams=2.0,
            timestamp=now - timedelta(hours=hours),
        )
        for suffix in ("x2k4p", "q8w2z")
        for hours in range(1, 4)
    ]
    return OptimizationContext(config=config, metrics=metrics, analysis_window_seconds=7 * 86400)


def _deps(config: Config) -> SourceDeps:
    return SourceDeps(config=config, cluster_uid=CLUSTER, clock=_now)


def _vpa(config: Config) -> VpaSignalSource:
    async def namespaces() -> List[str]:
        return ["shop"]

    connector = VpaSource(config, collector=FixtureCollector())
    return VpaSignalSource(SourceSettings(), _deps(config), connector=connector, namespace_lister=namespaces)


def _krr(config: Config) -> KrrSource:
    received = _now()
    result = parse_krr_report(
        json.loads((FIXTURES / "krr_report.json").read_text()),
        cluster_uid=CLUSTER,
        received_at=received,
        cluster_context="prod-eu",
    )
    inbox = MemoryInbox()
    inbox.seed(InboxBatch("krr-1", "krr", received, True, len(result.signals)), result.signals)
    return KrrSource(InboxSettings(), _deps(config), inbox=inbox)


def _native(config: Config):
    """GreenKube's own measurement of shop/api/web (250m, 512Mi): external sources never decide alone (A.5)."""
    from tests.core.optimization.test_engine_consensus import FakeConnector  # pyrefly: ignore[missing-import]

    common: Dict[str, Any] = dict(
        pod_name="api",
        container_name="web",
        namespace="shop",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="native",
    )
    return FakeConnector(
        "greenkube",
        [
            Recommendation(
                type=RecommendationType.RIGHTSIZING_CPU,
                current_cpu_request_millicores=1000,
                recommended_cpu_request_millicores=250,
                **common,
            ),
            Recommendation(
                type=RecommendationType.RIGHTSIZING_MEMORY,
                current_memory_request_bytes=1024 * MIB,
                recommended_memory_request_bytes=512 * MIB,
                **common,
            ),
        ],
    )


def _resources_signal(cpu: Optional[str] = None, memory: Optional[int] = None, **extra: Any) -> Dict[str, Any]:
    kind = "RIGHTSIZING_CPU" if cpu is not None else "RIGHTSIZING_MEMORY"
    proposed: Dict[str, Any] = {}
    current: Dict[str, Any] = {}
    if cpu is not None:
        proposed["cpu_request_m"], current["cpu_request_m"] = int(cpu), 1000
    if memory is not None:
        proposed["memory_request_bytes"], current["memory_request_bytes"] = memory, 1024 * MIB
    signal = {
        "target": {"kind": "Deployment", "namespace": "shop", "name": "api", "container": "web"},
        "capability": "container_requests",
        "kind": kind,
        "proposal": {"kind": "container_resources", "current": current, "proposed": proposed},
        "algorithm": {"name": "ci.rightsizer"},
    }
    signal.update(extra)
    return signal


def _deletion_signal(namespace: str, name: str, kind: str = "ZOMBIE_POD") -> Dict[str, Any]:
    return {
        "target": {"kind": "Deployment", "namespace": namespace, "name": name},
        "capability": "cleanup",
        "kind": kind,
        "proposal": {"kind": "deletion", "reason": "no traffic for 30 days"},
        "algorithm": {"name": "ci.janitor"},
    }


def _push(config: Config, batches: Dict[str, List[Dict[str, Any]]]) -> PushSignalSource:
    inbox = MemoryInbox()
    received = _now()
    for index, (client, signals) in enumerate(batches.items()):
        batch, parsed, errors = parse_batch(
            {"source_id": client, "complete": True, "signals": signals}, cluster_uid=CLUSTER, received_at=received
        )
        assert batch is not None and not errors, errors
        inbox.seed(InboxBatch(f"push-{index}", client, received, True, len(parsed)), parsed)
    return PushSignalSource(InboxSettings(), _deps(config), inbox=inbox)


async def _recommendations(sources) -> List[Recommendation]:
    config = _config()
    engine = OptimizationEngine(config=config, sources=[source(config) for source in sources])
    result = await engine.collect(_context(config))
    return result.recommendations


def _on_target(recs: List[Recommendation], rec_type: RecommendationType) -> Recommendation:
    [rec] = [
        rec
        for rec in recs
        if rec.namespace == "shop" and rec.owner_name == "api" and rec.container_name == "web" and rec.type == rec_type
    ]
    return rec


def _consensus(rec: Recommendation) -> Consensus:
    assert isinstance(rec.consensus, dict)
    return Consensus.model_validate(rec.consensus)


async def test_every_connector_becomes_one_consolidated_recommendation_per_resource():
    """Every external source votes next to GreenKube's own measurement (alone they stay signals, A.5)."""
    pushed = {"push:ci": [_resources_signal(cpu="260"), _resources_signal(memory=512 * MIB)]}
    recs = await _recommendations([_native, _vpa, _krr, lambda config: _push(config, pushed)])

    cpu = _on_target(recs, RecommendationType.RIGHTSIZING_CPU)
    consensus = _consensus(cpu)
    # The native measured value decides (B.3); Goldilocks' 300m only moves the confidence.
    assert cpu.recommended_cpu_request_millicores == 250
    assert consensus.value == 250 and consensus.policy == "native"
    assert consensus.spread["cpu_request_m"] == {
        "goldilocks": 300,
        "vpa": 250,
        "krr": 250,
        "push:ci": 260,
        "greenkube": 250,
    }
    # Goldilocks and the user VPA run one engine: one vote, not two. KRR's uploaded report
    # and the push client are both pushed through the API: together one vote. Plus GreenKube's own.
    assert consensus.independent_votes == 3
    assert {vote.family for vote in consensus.votes if vote.source_id in ("goldilocks", "vpa")} == {"vpa"}
    assert sum(vote.counted for vote in consensus.votes if vote.family == "vpa") == 1
    assert consensus.agreement == round(1 - 50 / 300, 4)
    # Provenance: every supporting source id, the chosen one first (UI badges).
    assert consensus.source_ids[0] == "greenkube"
    assert set(consensus.source_ids) == {"goldilocks", "vpa", "krr", "push:ci", "greenkube"}
    assert str(getattr(cpu.source, "value", cpu.source)) == "greenkube"

    memory = _on_target(recs, RecommendationType.RIGHTSIZING_MEMORY)
    memory_consensus = _consensus(memory)
    assert memory.recommended_memory_request_bytes == 512 * MIB
    assert memory_consensus.agreement == 1.0
    assert memory_consensus.independent_votes == 3
    assert set(memory_consensus.source_ids) == {"goldilocks", "vpa", "krr", "push:ci", "greenkube"}


async def test_a_disputed_vpa_object_still_reaches_consensus():
    """Two VPAs disagreeing on CPU produce no v1 recommendation; their signals still vote (G1)."""
    recs = await _recommendations([_native, _vpa])

    cpu = _on_target(recs, RecommendationType.RIGHTSIZING_CPU)
    consensus = _consensus(cpu)
    assert cpu.recommended_cpu_request_millicores == 250
    assert consensus.independent_votes == 2
    assert consensus.source_ids[0] == "greenkube"
    assert set(consensus.source_ids) == {"goldilocks", "vpa", "greenkube"}
    assert str(getattr(cpu.source, "value", cpu.source)) == "greenkube"


async def test_a_push_client_counts_once_whatever_engines_it_declares():
    pushed = {
        "push:ci": [
            _resources_signal(cpu="200", engine_family="engine-a"),
            _resources_signal(cpu="210", engine_family="engine-b"),
        ],
        "push:other": [_resources_signal(cpu="220")],
    }
    recs = await _recommendations([_native, _krr, lambda config: _push(config, pushed)])

    consensus = _consensus(_on_target(recs, RecommendationType.RIGHTSIZING_CPU))
    counted = [vote.source_id for vote in consensus.votes if vote.counted]
    assert sorted(counted) == ["greenkube", "krr", "push:ci", "push:other"]
    # Pushed sources never corroborate each other: one key can push as every client (plus GreenKube's vote).
    assert consensus.independent_votes == 2


async def test_a_push_client_relaying_vpa_cannot_replace_the_vpa_vote():
    pushed = {"push:relay": [_resources_signal(cpu="900", engine_family="vpa")]}
    recs = await _recommendations([_native, _vpa, lambda config: _push(config, pushed)])

    cpu = _on_target(recs, RecommendationType.RIGHTSIZING_CPU)
    consensus = _consensus(cpu)
    # The relay joins the VPA family: no extra vote, and VPA's own value stands for the family.
    assert consensus.independent_votes == 2
    assert consensus.spread["cpu_request_m"]["goldilocks"] == 300
    assert cpu.recommended_cpu_request_millicores == 250
    relay = next(vote for vote in consensus.votes if vote.source_id == "push:relay")
    assert relay.family == "vpa" and relay.counted is False


async def test_pushed_signals_alone_never_produce_a_deletion():
    pushed = {"push:janitor": [_deletion_signal("shop", "api")], "push:other": [_deletion_signal("shop", "api")]}
    recs = await _recommendations([lambda config: _push(config, pushed)])

    assert [rec for rec in recs if rec.type == RecommendationType.ZOMBIE_POD] == []


async def test_a_pushed_deletion_supports_a_deletion_another_source_proposes():
    from tests.core.optimization.test_engine_consensus import FakeConnector  # pyrefly: ignore[missing-import]

    native = Recommendation(
        pod_name="api",
        namespace="shop",
        type=RecommendationType.ZOMBIE_POD,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="Deployment shop/api serves no traffic.",
        reason="zombie",
    )
    pushed = {"push:janitor": [_deletion_signal("shop", "api")]}
    recs = await _recommendations(
        [lambda config: FakeConnector("greenkube", [native]), lambda config: _push(config, pushed)]
    )

    [zombie] = [rec for rec in recs if rec.type == RecommendationType.ZOMBIE_POD]
    consensus = _consensus(zombie)
    assert set(consensus.source_ids) == {"greenkube", "push:janitor"}
    assert str(getattr(zombie.source, "value", zombie.source)) == "greenkube"
