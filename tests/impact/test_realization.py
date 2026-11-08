"""RealizationContext: per-pool realization factors from the cluster's autoscalers (roadmap B6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from greenkube.collectors import autoscaler_collector
from greenkube.collectors.autoscaler_collector import AutoscalerCollector, karpenter_pool_from_item, pdb_from_object
from greenkube.core.config import Config
from greenkube.impact import ImpactProjector
from greenkube.impact.realization import (
    CLUSTER_POOL,
    SOURCE_CONFIG,
    SOURCE_CONTEXT,
    SOURCE_UNOBSERVED,
    ClusterAutoscalerObservation,
    KarpenterPoolObservation,
    NodeObservation,
    PdbObservation,
    PodObservation,
    RealizationInputs,
    current_realization_context,
    refresh_realization_context,
    resolve_realization,
    set_realization_context,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo

NOW = datetime.now(timezone.utc)
CA_TEXT = """Cluster-autoscaler status at 2026-01-01 10:00:00.000 +0000 UTC:
Cluster-wide:
  Health:      Healthy (ready=3 unready=0 notStarted=0 longNotStarted=0 registered=3 longUnregistered=0)
  ScaleUp:     NoActivity (ready=3 registered=3)
  ScaleDown:   NoCandidates (candidates=0)
"""
CA_YAML_UNHEALTHY = """time: "2026-01-01 10:00:00"
autoscalerStatus: Running
clusterWide:
  health:
    status: Unhealthy
    nodeCounts:
      registered:
        total: 3
"""


@pytest.fixture(autouse=True)
def _reset_context():
    set_realization_context(None)
    yield
    set_realization_context(None)


def _karpenter_node(name, pool="default", **annotations):
    return NodeObservation(name=name, labels={"karpenter.sh/nodepool": pool}, annotations=annotations)


def _pool(name="default", policy="WhenEmptyOrUnderutilized", budgets=()):
    return KarpenterPoolObservation(name=name, consolidation_policy=policy, budgets=tuple(budgets))


class TestHeuristic:
    def test_no_autoscaler_means_allocated_savings_only(self):
        context = resolve_realization(RealizationInputs(nodes=[NodeObservation("n1")]), NOW)
        assert context.factor_for("n1") == 0.0
        assert context.pool_for("n1").autoscaler == "none"
        assert "fixed nodes" in context.pool_for("n1").reasons[0]

    def test_cluster_autoscaler(self):
        inputs = RealizationInputs(
            nodes=[NodeObservation("n1")], cluster_autoscaler=ClusterAutoscalerObservation(CA_TEXT)
        )
        assert resolve_realization(inputs, NOW).factor_for("n1") == pytest.approx(0.6)

    @pytest.mark.parametrize(
        ("policy", "factor"),
        [("WhenEmptyOrUnderutilized", 0.8), ("Balanced", 0.8), ("WhenUnderutilized", 0.8), ("WhenEmpty", 0.3)],
    )
    def test_karpenter_consolidation_policies(self, policy, factor):
        inputs = RealizationInputs(nodes=[_karpenter_node("k1")], karpenter_pools=[_pool(policy=policy)])
        context = resolve_realization(inputs, NOW)
        assert context.factor_for("k1") == pytest.approx(factor)
        assert context.pool_for("k1").consolidation_policy == policy

    def test_pod_do_not_disrupt_halves_the_factor(self):
        inputs = RealizationInputs(
            nodes=[_karpenter_node("k1")],
            karpenter_pools=[_pool()],
            pods=[PodObservation("db", "pg-0", "k1", annotations={"karpenter.sh/do-not-disrupt": "true"})],
        )
        pool = resolve_realization(inputs, NOW).pool_for("k1")
        assert pool.factor == pytest.approx(0.4)
        assert pool.blocked
        assert any("do-not-disrupt" in reason for reason in pool.reasons)

    def test_blocking_pdb_only_affects_the_pools_of_its_pods(self):
        inputs = RealizationInputs(
            nodes=[_karpenter_node("k1"), NodeObservation("c1")],
            karpenter_pools=[_pool()],
            cluster_autoscaler=ClusterAutoscalerObservation(CA_TEXT),
            pods=[
                PodObservation("db", "pg-0", "c1", labels={"app": "pg"}),
                PodObservation("web", "api-0", "k1", labels={"app": "api"}),
            ],
            pdbs=[
                PdbObservation("db", "pg", match_labels={"app": "pg"}, disruptions_allowed=0, expected_pods=1),
                PdbObservation("web", "api", match_labels={"app": "api"}, disruptions_allowed=1, expected_pods=2),
            ],
        )
        context = resolve_realization(inputs, NOW)
        assert context.factor_for("k1") == pytest.approx(0.8)
        assert context.factor_for("c1") == pytest.approx(0.3)
        assert any("PodDisruptionBudget db/pg" in reason for reason in context.pool_for("c1").reasons)

    def test_pdb_match_expressions(self):
        pdb = PdbObservation("ns", "p", match_expressions=(("tier", "In", ("db",)), ("canary", "DoesNotExist", ())))
        assert pdb.selects(PodObservation("ns", "a", "n", labels={"tier": "db"}))
        assert not pdb.selects(PodObservation("ns", "b", "n", labels={"tier": "db", "canary": "1"}))
        assert not pdb.selects(PodObservation("other", "c", "n", labels={"tier": "db"}))

    def test_node_annotation_and_unhealthy_autoscaler_are_blockers(self):
        inputs = RealizationInputs(
            nodes=[NodeObservation("c1", annotations={"cluster-autoscaler.kubernetes.io/scale-down-disabled": "true"})],
            cluster_autoscaler=ClusterAutoscalerObservation(CA_TEXT),
        )
        assert resolve_realization(inputs, NOW).factor_for("c1") == pytest.approx(0.3)
        unhealthy = RealizationInputs(
            nodes=[NodeObservation("c1")], cluster_autoscaler=ClusterAutoscalerObservation(CA_YAML_UNHEALTHY)
        )
        pool = resolve_realization(unhealthy, NOW).pool_for("c1")
        assert pool.factor == pytest.approx(0.3)
        assert any("Unhealthy" in reason for reason in pool.reasons)

    def test_zero_disruption_budget_blocks_consolidation(self):
        always = RealizationInputs(nodes=[_karpenter_node("k1")], karpenter_pools=[_pool(budgets=[{"nodes": "0"}])])
        assert resolve_realization(always, NOW).factor_for("k1") == 0.0
        scheduled = RealizationInputs(
            nodes=[_karpenter_node("k1")],
            karpenter_pools=[_pool(budgets=[{"nodes": "0", "schedule": "0 9 * * mon-fri", "duration": "8h"}])],
        )
        assert resolve_realization(scheduled, NOW).factor_for("k1") == pytest.approx(0.4)
        drift_only = RealizationInputs(
            nodes=[_karpenter_node("k1")],
            karpenter_pools=[_pool(budgets=[{"nodes": "0", "reasons": ["Drifted"]}, {"nodes": "10%"}])],
        )
        assert resolve_realization(drift_only, NOW).factor_for("k1") == pytest.approx(0.8)

    def test_unreadable_cluster_autoscaler_status_is_not_no_autoscaler(self):
        """An RBAC or timeout error on the CA status keeps the unobserved factor, never 0.0."""
        unreadable = RealizationInputs(nodes=[NodeObservation("c1")], unavailable=("Cluster Autoscaler status",))
        pool = resolve_realization(unreadable, NOW).pool_for("c1")
        assert pool.factor == pytest.approx(1.0)
        assert pool.autoscaler == "unknown"
        assert any("could not be read" in reason for reason in pool.reasons)

        observed = resolve_realization(
            RealizationInputs(nodes=[NodeObservation("c1")], cluster_autoscaler=ClusterAutoscalerObservation(CA_TEXT)),
            NOW,
        )
        kept = resolve_realization(unreadable, NOW, previous=observed).pool_for("c1")
        assert kept.factor == pytest.approx(0.6)
        assert kept.autoscaler == "cluster_autoscaler"
        assert any("previous observation" in reason for reason in kept.reasons)

    def test_unreadable_karpenter_nodepools_keep_labeled_pools_out_of_the_cluster_pool(self):
        unreadable = RealizationInputs(
            nodes=[_karpenter_node("k1")], karpenter_pools=None, unavailable=("Karpenter NodePools",)
        )
        context = resolve_realization(unreadable, NOW)
        assert context.factor_for("k1") == pytest.approx(1.0)
        assert context.pool_for("k1").pool == "default"

        observed = resolve_realization(RealizationInputs(nodes=[_karpenter_node("k1")], karpenter_pools=[_pool()]), NOW)
        assert resolve_realization(unreadable, NOW, previous=observed).factor_for("k1") == pytest.approx(0.8)
        # The cluster pool itself was read fine: no autoscaler there.
        assert context.factor_for("unknown-node") == 0.0

    def test_unknown_nodes_use_the_cluster_pool(self):
        inputs = RealizationInputs(nodes=[_karpenter_node("k1")], karpenter_pools=[_pool()])
        context = resolve_realization(inputs, NOW)
        assert context.pool_for("new-node").pool == CLUSTER_POOL
        assert context.factor_for(None) == 0.0

    def test_cluster_autoscaler_health_parsing(self):
        assert ClusterAutoscalerObservation(CA_TEXT).health == "Healthy"
        assert ClusterAutoscalerObservation(CA_YAML_UNHEALTHY).health == "Unhealthy"
        assert ClusterAutoscalerObservation("garbage").health is None


def _row(node, minutes):
    return CombinedMetric(
        pod_name="api-0",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        node=node,
        cpu_request=1000,
        cpu_usage_millicores=50,
        memory_request=1024**3,
        timestamp=NOW - timedelta(hours=1) + timedelta(minutes=minutes),
        duration_seconds=300,
    )


class TestProjection:
    def _projector(self, config=None, rows=None):
        rows = rows or [_row("k1", 0), _row("k1", 5), _row("c1", 10), _row("c1", 15)]
        nodes = [NodeInfo(name="k1", cpu_capacity_cores=4), NodeInfo(name="c1", cpu_capacity_cores=4)]
        projector = ImpactProjector(config or Config(), node_infos=nodes, rows=rows, analysis_window_seconds=3600)
        return projector, rows

    def test_unobserved_cluster_keeps_allocated_expectations(self):
        projector, rows = self._projector()
        delta = projector.container_resources(
            projector.workload_ref("prod", "Deployment", "api"), rows, cpu_millicores=(1000, 100)
        )
        assert delta.realization_factor == 1.0
        assert delta.assumptions["realization_factor_source"] == SOURCE_UNOBSERVED
        assert delta.assumptions["realization_reasons"]

    def test_observed_context_weights_pools_by_hours(self):
        set_realization_context(
            resolve_realization(
                RealizationInputs(
                    nodes=[_karpenter_node("k1"), NodeObservation("c1")],
                    karpenter_pools=[_pool()],
                    cluster_autoscaler=ClusterAutoscalerObservation(CA_TEXT),
                ),
                NOW,
            )
        )
        projector, rows = self._projector()
        delta = projector.container_resources(
            projector.workload_ref("prod", "Deployment", "api"), rows, cpu_millicores=(1000, 100)
        )
        # Half the hours on the Karpenter pool (0.8), half on Cluster Autoscaler nodes (0.6).
        assert delta.realization_factor == pytest.approx(0.7)
        assert delta.expected_billable_cost_annual == pytest.approx(delta.allocated_cost_annual * 0.7)
        assert delta.co2e_expected_annual_g == pytest.approx(delta.co2e_allocated_annual_g * 0.7)
        assert delta.assumptions["realization_factor_source"] == SOURCE_CONTEXT
        assert {pool["pool"] for pool in delta.assumptions["realization_pools"]} == {"default", CLUSTER_POOL}
        assert any("Karpenter NodePool default" in reason for reason in delta.assumptions["realization_reasons"])

    def test_fixed_nodes_project_no_billable_saving(self):
        set_realization_context(resolve_realization(RealizationInputs(nodes=[NodeObservation("k1")]), NOW))
        projector, rows = self._projector(rows=[_row("k1", 0), _row("k1", 5)])
        delta = projector.container_resources(
            projector.workload_ref("prod", "Deployment", "api"), rows, cpu_millicores=(1000, 100)
        )
        assert delta.allocated_cost_annual > 0
        assert delta.expected_billable_cost_annual == 0.0
        assert delta.value_annual == 0.0

    def test_override_pins_the_factor(self):
        set_realization_context(resolve_realization(RealizationInputs(nodes=[NodeObservation("k1")]), NOW))
        projector, rows = self._projector(config=Config(IMPACT_REALIZATION_FACTOR=0.5))
        delta = projector.container_resources(
            projector.workload_ref("prod", "Deployment", "api"), rows, cpu_millicores=(1000, 100)
        )
        assert delta.realization_factor == 0.5
        assert delta.assumptions["realization_factor_source"] == SOURCE_CONFIG

    def test_deletions_stay_fully_realized(self):
        set_realization_context(resolve_realization(RealizationInputs(nodes=[NodeObservation("k1")]), NOW))
        projector, rows = self._projector()
        delta = projector.workload_deletion(projector.workload_ref("prod", "Deployment", "api"), rows)
        assert delta.realization_factor == 1.0


class _FakeCollector:
    def __init__(self, inputs):
        self.inputs = inputs
        self.calls = 0

    async def collect(self):
        self.calls += 1
        return self.inputs


class TestRefresh:
    async def test_refresh_caches_for_the_interval(self):
        collector = _FakeCollector(RealizationInputs(nodes=[NodeObservation("n1")]))
        first = await refresh_realization_context(Config(), collector=collector, now=NOW)
        assert first is not None and current_realization_context() is first
        await refresh_realization_context(Config(), collector=collector, now=NOW + timedelta(minutes=5))
        assert collector.calls == 1
        await refresh_realization_context(Config(), collector=collector, now=NOW + timedelta(minutes=11))
        assert collector.calls == 2

    async def test_failed_read_keeps_the_previous_context(self):
        await refresh_realization_context(
            Config(), collector=_FakeCollector(RealizationInputs(nodes=[NodeObservation("n1")])), now=NOW
        )
        previous = current_realization_context()
        result = await refresh_realization_context(Config(), collector=_FakeCollector(None), now=NOW, force=True)
        assert result is previous

    async def test_override_and_out_of_cluster_skip_the_read(self, monkeypatch):
        collector = _FakeCollector(RealizationInputs())
        await refresh_realization_context(Config(IMPACT_REALIZATION_FACTOR=0.2), collector=collector, now=NOW)
        assert collector.calls == 0
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        assert await refresh_realization_context(Config(), now=NOW) is None


class TestCollector:
    def test_karpenter_pool_parsing(self):
        pool = karpenter_pool_from_item(
            {
                "metadata": {"name": "general"},
                "spec": {"disruption": {"consolidationPolicy": "WhenEmpty", "budgets": [{"nodes": "10%"}]}},
            }
        )
        assert pool == KarpenterPoolObservation("general", "WhenEmpty", ({"nodes": "10%"},))
        assert karpenter_pool_from_item({"metadata": {}}) is None

    def test_pdb_parsing_from_client_models(self):
        pdb = SimpleNamespace(
            metadata=SimpleNamespace(namespace="db", name="pg"),
            spec=SimpleNamespace(
                selector=SimpleNamespace(
                    match_labels={"app": "pg"},
                    match_expressions=[SimpleNamespace(key="tier", operator="In", values=["db"])],
                )
            ),
            status=SimpleNamespace(disruptions_allowed=0, expected_pods=2),
        )
        parsed = pdb_from_object(pdb)
        assert parsed is not None and parsed.blocks
        assert parsed.match_expressions == (("tier", "In", ("db",)),)

    async def test_collect_reads_every_input(self, monkeypatch):
        node = SimpleNamespace(
            metadata=SimpleNamespace(name="k1", labels={"karpenter.sh/nodepool": "default"}, annotations={})
        )
        pod = SimpleNamespace(
            metadata=SimpleNamespace(
                namespace="db", name="pg-0", labels={"app": "pg"}, annotations={"karpenter.sh/do-not-disrupt": "true"}
            ),
            spec=SimpleNamespace(node_name="k1"),
        )
        core = SimpleNamespace(
            list_node=AsyncMock(return_value=SimpleNamespace(items=[node])),
            read_namespaced_config_map=AsyncMock(side_effect=type("NotFound", (Exception,), {"status": 404})()),
            list_pod_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[pod])),
        )
        policy = SimpleNamespace(
            list_pod_disruption_budget_for_all_namespaces=AsyncMock(return_value=SimpleNamespace(items=[]))
        )
        custom = SimpleNamespace(
            list_cluster_custom_object=AsyncMock(
                return_value={
                    "items": [
                        {"metadata": {"name": "default"}, "spec": {"disruption": {"consolidationPolicy": "Balanced"}}}
                    ]
                }
            )
        )
        monkeypatch.setattr(autoscaler_collector, "get_core_v1_api", AsyncMock(return_value=core))
        monkeypatch.setattr(autoscaler_collector, "get_policy_v1_api", AsyncMock(return_value=policy))
        monkeypatch.setattr(autoscaler_collector, "get_custom_objects_api", AsyncMock(return_value=custom))

        inputs = await AutoscalerCollector().collect()

        assert inputs is not None
        assert inputs.cluster_autoscaler is None
        assert inputs.unavailable == ()
        pool = resolve_realization(inputs, NOW).pool_for("k1")
        assert pool.factor == pytest.approx(0.4)

    async def test_collect_without_kubernetes_returns_none(self, monkeypatch):
        monkeypatch.setattr(autoscaler_collector, "get_core_v1_api", AsyncMock(return_value=None))
        assert await AutoscalerCollector().collect() is None
