"""Karpenter NodePool reads of the Karpenter collector."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from greenkube.collectors.karpenter_collector import KarpenterCollector

STATELESS_SPOT = [
    {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["on-demand", "spot"]},
    {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64", "arm64"]},
]


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
