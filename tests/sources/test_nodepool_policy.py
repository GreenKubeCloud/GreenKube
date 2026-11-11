"""NodePool policy advice of the greenkube.nodepool source (roadmap G5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from greenkube.collectors.karpenter_collector import KarpenterCollector, NodePoolInfo
from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.providers.karpenter import (
    KarpenterSource,
    NodePoolSignalSource,
    nodepool_policy_findings,
    parse_duration,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.signals.model import AdvisoryProposal, Capability
from greenkube.signals.sdk import SourceSettings
from tests.sources.contract import collect_context, deps  # pyrefly: ignore[missing-import]

STATELESS_SPOT = [
    {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["on-demand", "spot"]},
    {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64", "arm64"]},
]


def _context(owner_kind: str = "Deployment", nodes=("n1", "n2")) -> OptimizationContext:
    now = datetime.now(timezone.utc)
    metrics = [
        CombinedMetric(
            pod_name=f"pod-{node}",
            namespace="default",
            node=node,
            owner_kind=owner_kind,
            owner_name="app",
            cpu_request=2000,
            total_cost=1.0,
            timestamp=now,
        )
        for node in nodes
    ]
    return OptimizationContext(config=Config(), metrics=metrics, window_start=now - timedelta(days=1), window_end=now)


def _pool(
    policy: Optional[str] = "WhenEmptyOrUnderutilized",
    after: Optional[str] = None,
    requirements: Optional[List[dict]] = None,
) -> NodePoolInfo:
    return NodePoolInfo(
        name="general",
        node_names=["n1", "n2"],
        consolidation_policy=policy,
        consolidate_after=after,
        requirements=list(STATELESS_SPOT if requirements is None else requirements),
    )


def _kinds(pool: NodePoolInfo, context: Optional[OptimizationContext] = None) -> List[str]:
    return [finding["kind"] for finding in nodepool_policy_findings(pool, context or _context())]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("1h30m", timedelta(minutes=90)),
        ("Never", timedelta.max),
        ("soon", None),
        (None, None),
    ],
)
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


def test_well_configured_pool_has_no_advice():
    assert _kinds(_pool(after="30s")) == []


def test_when_empty_policy_is_advised():
    [finding] = nodepool_policy_findings(_pool("WhenEmpty"), _context())
    assert finding["kind"] == "NODEPOOL_CONSOLIDATION_POLICY"
    assert finding["patch"] == {"spec": {"disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized"}}}


@pytest.mark.parametrize(("after", "advised"), [("2h", True), ("Never", True), ("1h", False), ("5m", False)])
def test_excessive_consolidate_after(after, advised):
    assert ("NODEPOOL_CONSOLIDATE_AFTER" in _kinds(_pool(after=after))) is advised


def test_on_demand_only_stateless_pool_is_advised_to_allow_spot():
    on_demand = [
        {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["on-demand"]},
        STATELESS_SPOT[1],
    ]
    [finding] = nodepool_policy_findings(_pool(requirements=on_demand), _context())
    assert finding["kind"] == "NODEPOOL_CAPACITY_TYPE"
    assert finding["facts"]["workload_kinds"] == ["Deployment"]
    requirement = finding["patch"]["spec"]["template"]["spec"]["requirements"][0]
    assert requirement["values"] == ["on-demand", "spot"]


def test_stateful_or_unobserved_pools_are_not_advised_to_allow_spot():
    on_demand = [{"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["on-demand"]}, STATELESS_SPOT[1]]
    assert _kinds(_pool(requirements=on_demand), _context(owner_kind="StatefulSet")) == []
    assert _kinds(_pool(requirements=on_demand), _context(nodes=("elsewhere",))) == []


def test_single_architecture_is_advised_with_its_caveat():
    amd64_only = [STATELESS_SPOT[0], {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]}]
    [finding] = nodepool_policy_findings(_pool(requirements=amd64_only), _context())
    assert finding["kind"] == "NODEPOOL_ARCHITECTURE"
    assert "multi-arch" in finding["text"]
    # No requirement at all means Karpenter's amd64 default.
    assert "NODEPOOL_ARCHITECTURE" in _kinds(_pool(requirements=[STATELESS_SPOT[0]]))
    # NotIn and other operators are not second-guessed.
    not_in = [STATELESS_SPOT[0], {"key": "kubernetes.io/arch", "operator": "NotIn", "values": ["arm64"]}]
    assert _kinds(_pool(requirements=not_in)) == []


class _Collector:
    def __init__(self, pools):
        self.pools = pools

    async def collect(self):
        return self.pools


async def test_source_emits_high_risk_advisory_signals_without_v1_recommendations():
    pool = _pool("WhenEmpty", after="2h", requirements=[])
    source = NodePoolSignalSource(
        SourceSettings(), deps(), connector=KarpenterSource(Config(), collector=_Collector([pool]))
    )

    batch = await source.collect(collect_context(_context()))

    policy = [s for s in batch.signals if s.kind.startswith("NODEPOOL_")]
    assert sorted(s.kind for s in policy) == [
        "NODEPOOL_ARCHITECTURE",
        "NODEPOOL_CAPACITY_TYPE",
        "NODEPOOL_CONSOLIDATE_AFTER",
        "NODEPOOL_CONSOLIDATION_POLICY",
    ]
    for signal in policy:
        assert isinstance(signal.proposal, AdvisoryProposal)
        assert signal.capability == Capability.NODE_POOL
        assert signal.target.kind == "NodePool" and signal.target.name == "general"
        assert signal.evidence.data["risk_class"] == "high" and signal.evidence.data["advisory"] is True
        assert signal.evidence.data["suggested_patch"]
        assert signal.provenance.legacy_fingerprint is None
    assert batch.recommendations == []


async def test_namespaced_scans_skip_cluster_scoped_policy_advice():
    source = NodePoolSignalSource(
        SourceSettings(), deps(), connector=KarpenterSource(Config(), collector=_Collector([_pool("WhenEmpty")]))
    )
    batch = await source.collect(collect_context(_context(), namespace="default"))
    assert not [s for s in batch.signals if s.kind.startswith("NODEPOOL_")]


async def test_collector_reads_template_requirements_and_consolidate_after():
    """NodePool requirements live under spec.template.spec; the collector used to read spec.requirements."""
    pool = {
        "metadata": {"name": "general"},
        "spec": {
            "disruption": {"consolidationPolicy": "WhenEmpty", "consolidateAfter": "2h"},
            "template": {"spec": {"requirements": STATELESS_SPOT}},
        },
    }
    api = AsyncMock()
    api.list_cluster_custom_object = AsyncMock(side_effect=[{"items": []}, {"items": [pool]}, {"items": []}])
    collector = KarpenterCollector()
    with patch("greenkube.collectors.karpenter_collector.get_custom_objects_api", AsyncMock(return_value=api)):
        [info] = await collector.collect()
    assert info.consolidate_after == "2h"
    assert info.requirements == STATELESS_SPOT
