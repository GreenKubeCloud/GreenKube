"""Nodes sharing one physical host are charged the host once (NRG-15)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.collectors.node_collector import NodeCollector
from greenkube.core.config import Config
from greenkube.energy.node_power import per_core_watts
from greenkube.energy.shared_host import shared_host_groups
from greenkube.impact.rows import MetricRowAllocator, NodeSpec
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo

GIB = 1024**3
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
STEP_HOURS = 300 / 3600


def _kind_node(name: str, boot_id: str | None = "boot-1", provider_id: str | None = None) -> NodeInfo:
    return NodeInfo(
        name=name,
        cloud_provider="unknown",
        instance_type="cpu-14",
        cpu_capacity_cores=14,
        memory_capacity_bytes=39 * GIB,
        host_id=boot_id,
        provider_id=provider_id,
    )


def test_nodes_without_ids_take_the_ids_of_their_current_snapshot():
    historical = {name: _kind_node(name, boot_id=None) for name in ("cp", "w1")}
    current = {name: _kind_node(name) for name in ("cp", "w1")}
    assert shared_host_groups(historical) == {}
    groups = shared_host_groups(historical, current)
    assert {name: group.share for name, group in groups.items()} == pytest.approx({"cp": 0.5, "w1": 0.5})
    assert all(group.from_current_snapshot for group in groups.values())
    assert "current node snapshot" in groups["cp"].reason("cp")
    assert all(group.borrowed_ids for group in groups.values())
    # Borrowing is per node: a member with ids in force did not borrow.
    mixed = {"cp": _kind_node("cp"), "w1": _kind_node("w1", boot_id=None)}
    mixed_groups = shared_host_groups(mixed, current)
    assert (mixed_groups["cp"].borrowed_ids, mixed_groups["w1"].borrowed_ids) == (False, True)
    # Ids in force are never replaced by the current ones.
    moved = {"cp": _kind_node("cp", boot_id="old"), "w1": _kind_node("w1", boot_id="old")}
    assert not any(group.from_current_snapshot for group in shared_host_groups(moved, current).values())
    assert "current node snapshot" not in shared_host_groups(current)["cp"].reason("cp")


def test_nodes_with_the_same_boot_id_form_one_host():
    nodes = {name: _kind_node(name) for name in ("cp", "w1", "w2")}
    groups = shared_host_groups(nodes)
    shares = {name: group.share for name, group in groups.items()}
    assert shares == pytest.approx({"cp": 1 / 3, "w1": 1 / 3, "w2": 1 / 3})
    assert {group.size for group in groups.values()} == {3}


def test_distinct_boot_ids_are_unchanged():
    nodes = {"a": _kind_node("a", "boot-a"), "b": _kind_node("b", "boot-b")}
    assert shared_host_groups(nodes) == {}


def test_kind_provider_ids_group_nodes_without_a_boot_id():
    nodes = {name: _kind_node(name, None, f"kind://docker/kind/{name}") for name in ("cp", "w1")}
    assert {group.share for group in shared_host_groups(nodes).values()} == {0.5}


def _idle_pod(node: str) -> CombinedMetric:
    return CombinedMetric(
        pod_name=f"p-{node}",
        namespace="ns",
        node=node,
        timestamp=NOW,
        duration_seconds=300,
        cpu_request=0,
        memory_request=0,
        cpu_usage_millicores=0,
    )


def test_three_nodes_on_one_host_add_up_to_the_host_power_once():
    """Acceptance a: no load, Σ (pod + idle) joules == 14 × idle W/vCPU × step + memory power of 39 GiB once."""
    config = Config()
    allocator = MetricRowAllocator(config)
    nodes = {name: _kind_node(name) for name in ("cp", "w1", "w2")}
    groups = shared_host_groups(nodes)
    idle_per_core, max_per_core, _ = per_core_watts(None)
    total_joules = 0.0
    for name in nodes:
        spec = NodeSpec(
            name=name,
            cpu_capacity_cores=14,
            memory_capacity_bytes=39 * GIB,
            idle_watts=14 * idle_per_core,
            max_watts=14 * max_per_core,
            vcores=14,
            power_source="node_capacity_derived",
            embodied_g_per_hour=10.0,
            host_share=groups[name].share,
            host_reason=groups[name].reason(name),
        )
        rows = [_idle_pod(name)]
        profile = allocator.node_profile(spec, rows, pue=1.0, intensity=100.0, hours=STEP_HOURS)
        window = allocator.allocate(rows, profile, STEP_HOURS, emit_idle=True, idle_fields={"timestamp": NOW})
        assert window.idle is not None
        total_joules += sum(row.joules for row in window.rows) + window.idle.joules
        assert any(r.startswith("Shared host") for r in window.rows[0].estimation_reasons)
        assert window.idle.embodied_co2e_grams + sum(r.embodied_co2e_grams or 0 for r in window.rows) == pytest.approx(
            10.0 / 3 * STEP_HOURS
        )
    memory_watts = config.POWER_MEMORY_W_PER_GIB * 39
    assert total_joules == pytest.approx((14 * idle_per_core + memory_watts) * 300, rel=1e-9)


async def test_node_collector_reads_the_boot_id_and_provider_id():
    node = SimpleNamespace(
        metadata=SimpleNamespace(name="w1", labels={}, annotations={}),
        spec=SimpleNamespace(provider_id="kind://docker/kind/w1"),
        status=SimpleNamespace(capacity={"cpu": "14", "memory": "39Gi"}, node_info=SimpleNamespace(boot_id="boot-1")),
    )
    api = MagicMock()
    api.list_node = AsyncMock(return_value=SimpleNamespace(items=[node], metadata=SimpleNamespace(_continue=None)))
    collector = NodeCollector()
    collector._api = api
    info = (await collector.collect())["w1"]
    assert (info.host_id, info.provider_id) == ("boot-1", "kind://docker/kind/w1")
