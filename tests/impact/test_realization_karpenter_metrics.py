"""Karpenter Prometheus metrics feed the RealizationContext (roadmap G5, B6)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional
from unittest.mock import AsyncMock

from greenkube.collectors import autoscaler_collector
from greenkube.collectors.autoscaler_collector import AutoscalerCollector, karpenter_metric_queries, metric_by_label
from greenkube.impact.realization import (
    BLOCKER_MULTIPLIER,
    FACTOR_KARPENTER_CONSOLIDATING,
    FACTOR_KARPENTER_WHEN_EMPTY,
    UNAVAILABLE_KARPENTER_METRICS,
    KarpenterMetricsObservation,
    KarpenterPoolObservation,
    NodeObservation,
    RealizationInputs,
    resolve_realization,
)


def _inputs(metrics: Optional[KarpenterMetricsObservation]) -> RealizationInputs:
    return RealizationInputs(
        nodes=[
            NodeObservation("k1", labels={"karpenter.sh/nodepool": "general"}),
            NodeObservation("k2", labels={"karpenter.sh/nodepool": "batch"}),
        ],
        karpenter_pools=[
            KarpenterPoolObservation("general", "WhenEmptyOrUnderutilized"),
            KarpenterPoolObservation("batch", "WhenEmpty"),
        ],
        karpenter_metrics=metrics,
    )


def test_consolidation_decisions_are_reported_as_evidence():
    metrics = KarpenterMetricsObservation(decisions={"underutilized": 3.0, "empty": 1.0, "drifted": 5.0})
    general = resolve_realization(_inputs(metrics)).pools["general"]

    assert general.factor == FACTOR_KARPENTER_CONSOLIDATING and not general.blocked
    assert any("made 4 consolidation decision(s) over the last 7d" in reason for reason in general.reasons)


def test_no_consolidation_while_nodes_are_eligible_blocks_consolidating_pools():
    metrics = KarpenterMetricsObservation(
        decisions={"underutilized": 0.0, "drifted": 2.0}, eligible_nodes={"underutilized": 2.0}
    )
    context = resolve_realization(_inputs(metrics))

    general = context.pools["general"]
    assert general.blocked is True
    assert general.factor == FACTOR_KARPENTER_CONSOLIDATING * BLOCKER_MULTIPLIER
    assert any("no consolidation decision" in reason and "2 node(s)" in reason for reason in general.reasons)
    # WhenEmpty pools do not consolidate under-utilized nodes: the cluster-wide signal does not apply.
    assert context.pools["batch"].factor == FACTOR_KARPENTER_WHEN_EMPTY and not context.pools["batch"].blocked


def test_no_consolidation_without_eligible_nodes_changes_nothing():
    metrics = KarpenterMetricsObservation(decisions={"underutilized": 0.0}, eligible_nodes={})
    general = resolve_realization(_inputs(metrics)).pools["general"]

    assert general.factor == FACTOR_KARPENTER_CONSOLIDATING and not general.blocked
    assert any("no node is eligible now" in reason for reason in general.reasons)


def test_metrics_not_exported_are_never_read_as_zero():
    """Fail closed: an absent decisions series is no evidence that consolidation is stuck."""
    for metrics in (None, KarpenterMetricsObservation(decisions=None, eligible_nodes={"underutilized": 4.0})):
        general = resolve_realization(_inputs(metrics)).pools["general"]
        assert general.factor == FACTOR_KARPENTER_CONSOLIDATING and not general.blocked
        assert not any("consolidation decision" in reason for reason in general.reasons)


def test_pool_cost_is_recorded_per_pool():
    metrics = KarpenterMetricsObservation(pool_costs={"general": 41.5})
    context = resolve_realization(_inputs(metrics))

    assert context.pools["general"].observed_cost == 41.5
    assert context.pools["general"].to_dict()["observed_cost"] == 41.5
    assert "observed_cost" not in context.pools["batch"].to_dict()
    assert any("karpenter_nodepools_cost_total" in reason for reason in context.pools["general"].reasons)


def test_metric_parsing_sums_by_label_and_skips_bad_samples():
    result = [
        {"metric": {"reason": "Underutilized"}, "value": [0, "2"]},
        {"metric": {"reason": "underutilized"}, "value": [0, "1.5"]},
        {"metric": {"reason": "empty"}, "value": [0, "NaN"]},
        {"metric": {}, "value": [0, "9"]},
        {"value": "broken"},
    ]
    assert metric_by_label(result, "reason") == {"underutilized": 3.5}
    assert metric_by_label([], "reason") is None
    assert metric_by_label(None, "reason") is None
    assert metric_by_label([{"metric": {"nodepool": "General"}, "value": [0, "4"]}], "nodepool") == {"General": 4.0}


def _cluster(monkeypatch) -> None:
    node = SimpleNamespace(
        metadata=SimpleNamespace(name="k1", labels={"karpenter.sh/nodepool": "general"}, annotations={})
    )
    core = SimpleNamespace(
        list_node=AsyncMock(return_value=SimpleNamespace(items=[node])),
        read_namespaced_config_map=AsyncMock(side_effect=type("NotFound", (Exception,), {"status": 404})()),
        list_pod_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[])),
        list_event_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[])),
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
        return {"items": []}

    monkeypatch.setattr(autoscaler_collector, "get_core_v1_api", AsyncMock(return_value=core))
    monkeypatch.setattr(autoscaler_collector, "get_policy_v1_api", AsyncMock(return_value=policy))
    monkeypatch.setattr(
        autoscaler_collector,
        "get_custom_objects_api",
        AsyncMock(return_value=SimpleNamespace(list_cluster_custom_object=custom_objects)),
    )


async def test_collector_reads_the_karpenter_metrics(monkeypatch):
    _cluster(monkeypatch)
    answers: Dict[str, List[dict]] = {
        karpenter_metric_queries()["decisions"]: [{"metric": {"reason": "underutilized"}, "value": [0, "0"]}],
        karpenter_metric_queries()["eligible_nodes"]: [{"metric": {"reason": "underutilized"}, "value": [0, "3"]}],
        karpenter_metric_queries()["pool_costs"]: [],
    }
    asked: List[str] = []

    async def query(promql: str):
        asked.append(promql)
        return answers[promql]

    inputs = await AutoscalerCollector(metrics_query=query).collect()

    assert inputs is not None and inputs.unavailable == ()
    assert inputs.karpenter_metrics == KarpenterMetricsObservation(
        decisions={"underutilized": 0.0}, eligible_nodes={"underutilized": 3.0}, pool_costs=None
    )
    assert len(asked) == 3
    assert resolve_realization(inputs).pools["general"].blocked is True


async def test_unreadable_metrics_are_noted_and_ignored(monkeypatch):
    _cluster(monkeypatch)

    async def failing(_promql: str):
        return None

    inputs = await AutoscalerCollector(metrics_query=failing).collect()

    assert inputs is not None and inputs.karpenter_metrics is None
    assert inputs.unavailable == (UNAVAILABLE_KARPENTER_METRICS,)
    general = resolve_realization(inputs).pools["general"]
    assert general.factor == FACTOR_KARPENTER_CONSOLIDATING and not general.blocked


async def test_without_prometheus_the_metrics_are_not_read(monkeypatch):
    _cluster(monkeypatch)
    monkeypatch.setattr(autoscaler_collector.global_config, "PROMETHEUS_URL", "")

    inputs = await AutoscalerCollector().collect()

    assert inputs is not None and inputs.karpenter_metrics is None and inputs.unavailable == ()
