"""Karpenter events and NodeClaims feed the RealizationContext (roadmap G5, B6)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from greenkube.collectors import autoscaler_collector
from greenkube.collectors.autoscaler_collector import (
    UNAVAILABLE_KARPENTER_EVENTS,
    AutoscalerCollector,
    karpenter_event_from_object,
)
from greenkube.impact.realization import (
    BLOCKER_MULTIPLIER,
    FACTOR_KARPENTER_CONSOLIDATING,
    KarpenterEventObservation,
    KarpenterPoolObservation,
    NodeObservation,
    RealizationInputs,
    resolve_realization,
)


def _inputs(*events: KarpenterEventObservation) -> RealizationInputs:
    return RealizationInputs(
        nodes=[
            NodeObservation("k1", labels={"karpenter.sh/nodepool": "general"}),
            NodeObservation("k2", labels={"karpenter.sh/nodepool": "spot"}),
        ],
        karpenter_pools=[
            KarpenterPoolObservation("general", "WhenEmptyOrUnderutilized"),
            KarpenterPoolObservation("spot", "WhenEmptyOrUnderutilized"),
        ],
        karpenter_events=list(events),
    )


def test_disruption_blocked_event_blocks_its_pool():
    context = resolve_realization(
        _inputs(
            KarpenterEventObservation("DisruptionBlocked", "NodeClaim", "general-abc", pool="general", message="pdb")
        )
    )
    general = context.pools["general"]
    assert general.blocked is True
    assert general.factor == FACTOR_KARPENTER_CONSOLIDATING * BLOCKER_MULTIPLIER
    assert any("DisruptionBlocked on NodeClaim general-abc: pdb" in reason for reason in general.reasons)
    assert context.pools["spot"].blocked is False


def test_node_events_map_through_the_node_pool_label():
    context = resolve_realization(_inputs(KarpenterEventObservation("DisruptionBlocked", "Node", "k2")))
    assert context.pools["spot"].blocked is True
    assert context.pools["general"].blocked is False


def test_unconsolidatable_is_reported_without_changing_the_factor():
    context = resolve_realization(
        _inputs(
            KarpenterEventObservation("Unconsolidatable", "Node", "k1"),
            KarpenterEventObservation("Unconsolidatable", "NodePool", "general", pool="general"),
        )
    )
    general = context.pools["general"]
    assert general.factor == FACTOR_KARPENTER_CONSOLIDATING and not general.blocked
    assert "Karpenter reports 2 object(s) of NodePool general as Unconsolidatable" in general.reasons


def test_events_about_unknown_objects_are_ignored():
    context = resolve_realization(_inputs(KarpenterEventObservation("DisruptionBlocked", "Node", "unknown-node")))
    assert not any(pool.blocked for pool in context.pools.values())


def test_event_parsing_resolves_nodeclaims_and_skips_other_objects():
    claim = SimpleNamespace(
        involved_object=SimpleNamespace(kind="NodeClaim", name="general-abc"), reason="DisruptionBlocked", message="m"
    )
    pod = SimpleNamespace(involved_object=SimpleNamespace(kind="Pod", name="x"), reason="DisruptionBlocked", message="")
    parsed = karpenter_event_from_object(claim, {"general-abc": "general"})
    assert parsed == KarpenterEventObservation("DisruptionBlocked", "NodeClaim", "general-abc", "general", "m")
    assert karpenter_event_from_object(pod, {}) is None


async def test_collector_reads_events_and_claims_when_karpenter_runs(monkeypatch):
    node = SimpleNamespace(
        metadata=SimpleNamespace(name="k1", labels={"karpenter.sh/nodepool": "general"}, annotations={})
    )
    event = SimpleNamespace(
        involved_object=SimpleNamespace(kind="NodeClaim", name="general-abc"), reason="DisruptionBlocked", message="pdb"
    )

    async def events(field_selector, **_kwargs):
        return SimpleNamespace(items=[event] if field_selector == "reason=DisruptionBlocked" else [])

    core = SimpleNamespace(
        list_node=AsyncMock(return_value=SimpleNamespace(items=[node])),
        read_namespaced_config_map=AsyncMock(side_effect=type("NotFound", (Exception,), {"status": 404})()),
        list_pod_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[])),
        list_event_for_all_namespaces=events,
    )
    policy = SimpleNamespace(
        list_pod_disruption_budget_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    async def custom_objects(group, version, plural, **_kwargs):
        if plural == "nodepools":
            return {
                "items": [
                    {"metadata": {"name": "general"}, "spec": {"disruption": {"consolidationPolicy": "Balanced"}}}
                ]
            }
        return {"items": [{"metadata": {"name": "general-abc", "labels": {"karpenter.sh/nodepool": "general"}}}]}

    custom = SimpleNamespace(list_cluster_custom_object=custom_objects)
    monkeypatch.setattr(autoscaler_collector, "get_core_v1_api", AsyncMock(return_value=core))
    monkeypatch.setattr(autoscaler_collector, "get_policy_v1_api", AsyncMock(return_value=policy))
    monkeypatch.setattr(autoscaler_collector, "get_custom_objects_api", AsyncMock(return_value=custom))

    inputs = await AutoscalerCollector().collect()

    assert inputs is not None and inputs.unavailable == ()
    assert inputs.karpenter_events == [
        KarpenterEventObservation("DisruptionBlocked", "NodeClaim", "general-abc", "general", "pdb")
    ]
    assert resolve_realization(inputs).pools["general"].blocked is True


async def test_unreadable_events_are_reported_not_ignored(monkeypatch):
    node = SimpleNamespace(
        metadata=SimpleNamespace(name="k1", labels={"karpenter.sh/nodepool": "general"}, annotations={})
    )
    core = SimpleNamespace(
        list_node=AsyncMock(return_value=SimpleNamespace(items=[node])),
        read_namespaced_config_map=AsyncMock(side_effect=type("NotFound", (Exception,), {"status": 404})()),
        list_pod_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[])),
        list_event_for_all_namespaces=AsyncMock(side_effect=type("Forbidden", (Exception,), {"status": 403})()),
    )
    policy = SimpleNamespace(
        list_pod_disruption_budget_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[]))
    )
    custom = SimpleNamespace(
        list_cluster_custom_object=AsyncMock(
            return_value={"items": [{"metadata": {"name": "general"}, "spec": {"disruption": {}}}]}
        )
    )
    monkeypatch.setattr(autoscaler_collector, "get_core_v1_api", AsyncMock(return_value=core))
    monkeypatch.setattr(autoscaler_collector, "get_policy_v1_api", AsyncMock(return_value=policy))
    monkeypatch.setattr(autoscaler_collector, "get_custom_objects_api", AsyncMock(return_value=custom))

    inputs = await AutoscalerCollector().collect()

    assert inputs is not None and inputs.karpenter_events is None
    assert UNAVAILABLE_KARPENTER_EVENTS in inputs.unavailable
    assert "Karpenter events could not be read" in resolve_realization(inputs).notes
