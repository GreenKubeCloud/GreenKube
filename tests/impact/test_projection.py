# tests/impact/test_projection.py
"""Projection rules of the Impact Model (roadmap B2/B4) and BUG-15/BUG-16 regressions."""

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from greenkube.collectors.karpenter_collector import NodePoolInfo
from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer, MemoryRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.providers.karpenter import KarpenterSource
from greenkube.impact import (
    IMPACT_MODEL_VERSION,
    ContainerResourceChange,
    CostSource,
    ImpactProjector,
    NodeImpactProfile,
    NodePoolChange,
    NodePoolContext,
    PricingCatalog,
    ReplicaScheduleChange,
    ReservationAwareImpactModel,
    ResourceSample,
    UnitPrices,
    WorkloadBaseline,
    WorkloadDeletion,
    WorkloadRef,
    carbon_value,
)
from greenkube.impact.types import HOURS_PER_YEAR
from greenkube.models.metrics import CombinedMetric, RecommendationType
from greenkube.models.node import NodeInfo

MIB = 1024**2
GIB = 1024**3
START = datetime(2026, 11, 1, tzinfo=timezone.utc)


class CostedMetric(CombinedMetric):
    """A metric row carrying per-resource allocated costs (roadmap A4/A11 rows)."""

    cost_cpu: Optional[float] = None
    cost_ram: Optional[float] = None
    cost_source: Optional[str] = None


def _profile(name="n1", cores=4.0, mem=16.0, realization=1.0):
    node = NodeImpactProfile(
        name=name,
        cpu_capacity_cores=cores,
        mem_capacity_gib=mem,
        idle_watts=40.0,
        max_watts=120.0,
        embodied_g_per_hour=10.0,
        prices=UnitPrices(0.04, 0.005, "USD", CostSource.PRICING_CATALOG, "fixture"),
        pue=1.2,
        grid_intensity_g_per_kwh=300.0,
    )
    pool = NodePoolContext(
        nodes={name: node},
        default_node=node,
        realization_factor=realization,
        carbon_price_per_tonne=100.0,
        currency="USD",
    )
    return node, pool


def _baseline(samples, window_hours=24.0):
    return WorkloadBaseline(
        ref=WorkloadRef("c", "ns", "Deployment", "api", "api"), samples=samples, window_hours=window_hours
    )


def _sample(cpu_req, cpu_use, mem_req, mem_use, hours=1.0, node="n1", pue=None, intensity=None):
    return ResourceSample(
        ref=WorkloadRef("c", "ns", "Deployment", "api", "api"),
        hours=hours,
        cpu_request_cores=cpu_req,
        cpu_usage_cores=cpu_use,
        mem_request_gib=mem_req,
        mem_usage_gib=mem_use,
        node=node,
        pue=pue,
        grid_intensity_g_per_kwh=intensity,
    )


MODEL = ReservationAwareImpactModel()


def _impact(rec) -> dict:
    assert rec.impact is not None
    return rec.impact


def _savings(rec) -> float:
    assert rec.potential_savings_cost is not None
    return rec.potential_savings_cost


def _co2e(rec) -> float:
    assert rec.potential_savings_co2e_grams is not None
    return rec.potential_savings_co2e_grams


class TestContainerRequestProperties:
    @pytest.mark.parametrize("seed", range(40))
    def test_cpu_plus_memory_delta_never_exceeds_allocated_container_cost(self, seed):
        """B4 acceptance: projected CPU + memory cost delta ≤ allocated container cost."""
        rng = random.Random(seed)
        _, pool = _profile(realization=rng.uniform(0, 1))
        cpu_req = rng.uniform(0.05, 4)
        mem_req = rng.uniform(0.05, 16)
        samples = [
            _sample(
                cpu_req,
                rng.choice([None, rng.uniform(0, 2 * cpu_req)]),
                mem_req,
                rng.choice([None, rng.uniform(0, 2 * mem_req)]),
                hours=rng.choice([1 / 12, 1.0]),
            )
            for _ in range(rng.randint(1, 30))
        ]
        baseline = _baseline(samples, window_hours=rng.choice([24.0, 168.0]))
        change = ContainerResourceChange(
            cpu_request_cores=(cpu_req, cpu_req * rng.uniform(0, 1)),
            mem_request_gib=(mem_req, mem_req * rng.uniform(0, 1)),
        )

        combined = MODEL.project(baseline, change, pool)
        allocated = MODEL.project(baseline, WorkloadDeletion(), pool)
        cpu_only = MODEL.project(baseline, ContainerResourceChange(cpu_request_cores=change.cpu_request_cores), pool)
        mem_only = MODEL.project(baseline, ContainerResourceChange(mem_request_gib=change.mem_request_gib), pool)

        assert 0 <= combined.allocated_cost_annual <= allocated.allocated_cost_annual + 1e-9
        assert combined.expected_billable_cost_annual <= combined.allocated_cost_annual + 1e-12
        # CPU and memory deltas are additive, so two recommendations on one
        # container can never claim more than the container is charged.
        assert cpu_only.allocated_cost_annual + mem_only.allocated_cost_annual == pytest.approx(
            combined.allocated_cost_annual
        )

    def test_cpu_only_cut_projects_zero_ram_savings(self):
        _, pool = _profile()
        delta = MODEL.project(
            _baseline([_sample(2, 0.1, 8, 1)]),
            ContainerResourceChange(cpu_request_cores=(2, 0.5)),
            pool,
        )
        assert delta.cost_breakdown_annual.ram == 0.0
        assert delta.capacity_released.mem_gib_hours == 0.0
        assert delta.cost_breakdown_annual.cpu > 0

    def test_request_cut_co2e_is_idle_and_embodied_share_released_times_realization(self):
        """B4 acceptance (BUG-16): CO2e of a request cut = (idle + embodied) released × realization."""
        node, pool = _profile(realization=0.6)
        samples = [_sample(2, 0.2, 8, 1, pue=1.5, intensity=200.0), _sample(2, 0.8, 8, 1)]
        delta = MODEL.project(_baseline(samples, 24.0), ContainerResourceChange(cpu_request_cores=(2, 1)), pool)

        released_core_hours = 1.0 * 1.0 + 1.0 * 1.0  # 1 core per sample, one hour each
        annual = HOURS_PER_YEAR / 24.0
        idle_w_per_core = node.idle_watts / node.cpu_capacity_cores
        idle_g = (1.0 * idle_w_per_core / 1000 * 1.5 * 200.0) + (1.0 * idle_w_per_core / 1000 * 1.2 * 300.0)
        embodied_g = released_core_hours * node.embodied_g_per_hour / node.cpu_capacity_cores
        expected_allocated = (idle_g + embodied_g) * annual

        assert delta.co2e_breakdown_annual_g.dynamic_operational == 0.0
        assert delta.co2e_allocated_annual_g == pytest.approx(expected_allocated)
        assert delta.co2e_expected_annual_g == pytest.approx(expected_allocated * 0.6)
        assert delta.expected_billable_cost_annual == pytest.approx(delta.allocated_cost_annual * 0.6)

    def test_usage_above_target_limits_released_reservation(self):
        """Reservation is max(request, usage): a target below usage releases only down to usage."""
        _, pool = _profile()
        delta = MODEL.project(
            _baseline([_sample(2, 1.5, 0, None)], 1.0),
            ContainerResourceChange(cpu_request_cores=(2, 1)),
            pool,
        )
        assert delta.capacity_released.cpu_core_hours == pytest.approx(0.5 * HOURS_PER_YEAR)

    def test_value_annual_unifies_cost_and_carbon(self):
        _, pool = _profile()
        delta = MODEL.project(
            _baseline([_sample(2, 0.1, 8, 1)]), ContainerResourceChange(cpu_request_cores=(2, 1)), pool
        )
        assert delta.carbon_price_per_tonne == 100.0
        assert delta.value_annual == pytest.approx(
            delta.expected_billable_cost_annual + 100.0 * delta.co2e_expected_annual_g / 1e6
        )
        assert carbon_value(10.0, 2e6, 50.0) == pytest.approx(110.0)

    def test_delta_serializes_with_version_and_method(self):
        _, pool = _profile()
        delta = MODEL.project(
            _baseline([_sample(2, 0.1, 8, 1)]), ContainerResourceChange(cpu_request_cores=(2, 1)), pool
        )
        payload = delta.to_dict()
        assert payload["model_version"] == IMPACT_MODEL_VERSION
        assert payload["method"] == "projected"
        assert payload["kind"] == "container_resources"
        assert payload["assumptions"]["unit_prices"][0]["source"] == "pricing_catalog"
        assert payload["assumptions"]["change"] == {"cpu_request_cores": [2, 1]}


class TestOtherChanges:
    def test_workload_deletion_uses_observed_allocated_cost_when_known(self):
        _, pool = _profile()
        sample = ResourceSample(
            ref=WorkloadRef("c", "ns", "Pod", "zombie"),
            hours=1.0,
            cpu_request_cores=1.0,
            cpu_usage_cores=0.0,
            node="n1",
            observed_cost_total=0.5,
            observed_cost_cpu=0.3,
            observed_cost_ram=0.1,
        )
        delta = MODEL.project(_baseline([sample], 24.0), WorkloadDeletion(), pool)
        annual = HOURS_PER_YEAR / 24.0
        assert delta.allocated_cost_annual == pytest.approx(0.5 * annual)
        assert delta.cost_breakdown_annual.cpu == pytest.approx(0.3 * annual)
        assert delta.cost_breakdown_annual.other == pytest.approx(0.1 * annual)
        assert delta.realization_factor == 1.0

    def test_off_peak_schedule_removes_only_idle_hours(self):
        _, pool = _profile(realization=0.5)
        samples = [replace(_sample(1, 0.5, 2, 1), timestamp=START + timedelta(hours=hour)) for hour in range(24)]
        baseline = _baseline(samples, 24.0)
        delta = MODEL.project(baseline, ReplicaScheduleChange(frozenset({0, 1, 2, 3})), pool)
        full = MODEL.project(baseline, WorkloadDeletion(), pool)
        assert delta.allocated_cost_annual == pytest.approx(full.allocated_cost_annual * 4 / 24)
        assert delta.realization_factor == 0.5
        assert delta.capacity_released.replica_hours == pytest.approx(4 * HOURS_PER_YEAR / 24)

    def test_node_removal_prices_whole_nodes(self):
        node, pool = _profile()
        delta = MODEL.project(_baseline([]), NodePoolChange(("n1",)), pool)
        assert delta.allocated_cost_annual == pytest.approx(node.hourly_price * HOURS_PER_YEAR)
        assert delta.capacity_released.node_hours == HOURS_PER_YEAR
        assert delta.realization_factor == 1.0
        assert delta.co2e_breakdown_annual_g.dynamic_operational == 0.0
        assert delta.co2e_breakdown_annual_g.embodied == pytest.approx(10.0 * HOURS_PER_YEAR)


# --------------------------------------------------------------------------- #
# Analyzer-level regressions                                                   #
# --------------------------------------------------------------------------- #


def _container_rows(count=12, **overrides) -> List[CombinedMetric]:
    rows: List[CombinedMetric] = []
    for index in range(count):
        values = dict(
            pod_name="api-7f9c5d9f6d-a1b2c",
            namespace="prod",
            owner_kind="Deployment",
            owner_name="api",
            container_name="api",
            node="n1",
            cpu_request=1000,
            cpu_usage_millicores=50,
            memory_request=1 * GIB,
            memory_usage_bytes=64 * MIB,
            total_cost=0.010,
            co2e_grams=5.0,
            grid_intensity=300.0,
            pue=1.2,
            timestamp=START + timedelta(minutes=5 * index),
            duration_seconds=300,
        )
        values.update(overrides)
        rows.append(CostedMetric(**values))
    return rows


def _analyze(rows: list, node_infos=(), window=3600.0, config=None):
    config = config or Config()
    context = OptimizationContext(
        config=config, metrics=rows, node_infos=list(node_infos), analysis_window_seconds=window
    )
    return [
        *CpuRightsizingAnalyzer(config).analyze(context),
        *MemoryRightsizingAnalyzer(config).analyze(context),
    ]


class TestBug15:
    def test_cpu_and_memory_recommendations_never_exceed_container_cost(self):
        """BUG-15: both rightsizing cuts on one container used to claim ~90% of the total cost each."""
        # Observed OpenCost split of the 0.01/5 min total: 0.008 CPU, 0.002 RAM.
        rows = _container_rows(cost_cpu=0.008, cost_ram=0.002, cost_source="opencost")
        recs = _analyze(rows)
        by_type = {rec.type: rec for rec in recs}
        assert set(by_type) == {RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY}

        annual_container_cost = sum(row.total_cost for row in rows) * HOURS_PER_YEAR
        total_projected = sum(_savings(rec) for rec in recs)
        assert total_projected <= annual_container_cost
        # The old formula (annual total cost × request-cut ratio, once per
        # resource) claimed more than 100% of the container cost here.
        cpu_ratio = 1 - (by_type[RecommendationType.RIGHTSIZING_CPU].recommended_cpu_request_millicores or 0) / 1000
        mem_ratio = 1 - (by_type[RecommendationType.RIGHTSIZING_MEMORY].recommended_memory_request_bytes or 0) / GIB
        assert annual_container_cost * (cpu_ratio + mem_ratio) > annual_container_cost

        cpu = by_type[RecommendationType.RIGHTSIZING_CPU]
        memory = by_type[RecommendationType.RIGHTSIZING_MEMORY]
        assert _impact(cpu)["cost_breakdown_annual"]["ram"] == 0.0
        assert _impact(memory)["cost_breakdown_annual"]["cpu"] == 0.0
        # CPU savings come only from the observed CPU cost (0.008 per row).
        assert _savings(cpu) <= 0.008 * len(rows) * HOURS_PER_YEAR
        assert _impact(cpu)["assumptions"]["unit_prices"][0]["source"] == "opencost"

    def test_catalog_prices_are_used_without_observed_component_costs(self):
        node = NodeInfo(
            name="n1",
            instance_type="m5.large",
            region="eu-west-3",
            cloud_provider="aws",
            cpu_capacity_cores=2,
            memory_capacity_bytes=8 * GIB,
        )
        catalog_doc = {
            "schema_version": 1,
            "provider": "aws",
            "currency": "USD",
            "columns": ["hourly", "vcpu", "memory_gib"],
            "regions": {"eu-west-3": {"m5.large": [0.112, 2, 8]}},
        }
        context = OptimizationContext(
            config=Config(), metrics=_container_rows(), node_infos=[node], analysis_window_seconds=3600.0
        )
        projector = ImpactProjector(
            Config(),
            node_infos=[node],
            rows=context.metrics,
            analysis_window_seconds=3600.0,
            catalog=PricingCatalog(documents={"aws": catalog_doc}),
        )
        delta = projector.container_resources(
            projector.workload_ref("prod", "Deployment", "api", "api"),
            context.metrics,
            cpu_millicores=(1000, 100),
        )
        prices = delta.assumptions["unit_prices"][0]
        assert prices["source"] == "pricing_catalog"
        assert prices["detail"] == "aws/eu-west-3/m5.large"
        assert delta.cost_breakdown_annual.cpu == pytest.approx(0.9 * 1.0 * HOURS_PER_YEAR * prices["cpu_core_hour"])


class TestBug16:
    def test_cpu_cut_co2e_ignores_usage_based_row_emissions(self):
        """BUG-16: request cuts projected CO2e ∝ usage-based emissions, which requests do not change."""
        small = _analyze(_container_rows(co2e_grams=0.001))
        large = _analyze(_container_rows(co2e_grams=1000.0))
        small_cpu = next(rec for rec in small if rec.type == RecommendationType.RIGHTSIZING_CPU)
        large_cpu = next(rec for rec in large if rec.type == RecommendationType.RIGHTSIZING_CPU)

        assert small_cpu.potential_savings_co2e_grams == pytest.approx(large_cpu.potential_savings_co2e_grams)
        breakdown = _impact(small_cpu)["co2e_breakdown_annual_g"]
        assert breakdown["dynamic_operational"] == 0.0
        assert breakdown["idle_operational"] > 0
        assert breakdown["embodied"] > 0

    def test_memory_cut_projects_no_co2e_until_memory_power_is_modeled(self):
        recs = _analyze(_container_rows())
        memory = next(rec for rec in recs if rec.type == RecommendationType.RIGHTSIZING_MEMORY)
        assert memory.potential_savings_co2e_grams == 0.0

    def test_realization_factor_scales_expected_values(self):
        recs = _analyze(_container_rows(), config=Config(IMPACT_REALIZATION_FACTOR=0.25))
        cpu = next(rec for rec in recs if rec.type == RecommendationType.RIGHTSIZING_CPU)
        assert _impact(cpu)["realization_factor"] == 0.25
        assert _savings(cpu) == pytest.approx(_impact(cpu)["allocated_cost_annual"] * 0.25)
        assert _co2e(cpu) == pytest.approx(_impact(cpu)["co2e_allocated_annual_g"] * 0.25)


class TestKarpenterProjection:
    async def test_consolidation_projects_removed_node_hours(self):
        nodes = [
            NodeInfo(name=f"n{i}", cpu_capacity_cores=4, memory_capacity_bytes=16 * GIB, cloud_provider="aws")
            for i in range(3)
        ]
        rows = [
            CombinedMetric(pod_name=f"p{i}", namespace="default", node=f"n{i}", cpu_request=200, timestamp=START)
            for i in range(3)
        ]
        context = OptimizationContext(config=Config(), metrics=rows, node_infos=nodes, analysis_window_seconds=86400)

        class _Collector:
            async def collect(self):
                return [NodePoolInfo(name="default", node_names=["n0", "n1", "n2"])]

        recs = await KarpenterSource(Config(), collector=_Collector()).collect(context)

        assert len(recs) == 1
        impact = _impact(recs[0])
        # 5% request utilization on 3 nodes: one node remains, two are removable.
        assert impact["capacity_released"]["node_hours"] == 2 * HOURS_PER_YEAR
        node_price = 4 * 0.031611 + 16 * 0.004237
        assert recs[0].potential_savings_cost == pytest.approx(2 * node_price * HOURS_PER_YEAR)
        assert impact["realization_factor"] == 1.0
        assert _co2e(recs[0]) > 0


class TestScheduleAndDeletionAnalyzers:
    def _context(self, rows, window=86400.0, **extra):
        return OptimizationContext(config=Config(), metrics=rows, analysis_window_seconds=window, **extra)

    def test_off_peak_projects_removed_replica_hours(self):
        from greenkube.core.optimization.analyzers import OffPeakAnalyzer

        rows = [
            CombinedMetric(
                pod_name="batch-7f9c5d9f6d-a1b2c",
                namespace="jobs",
                owner_kind="Deployment",
                owner_name="batch",
                node="n1",
                cpu_request=500,
                memory_request=GIB,
                cpu_usage_millicores=1 if hour < 8 else 400,
                total_cost=0.01,
                timestamp=START + timedelta(hours=hour),
                duration_seconds=3600,
            )
            for hour in range(24)
        ]
        (rec,) = OffPeakAnalyzer(Config()).analyze(self._context(rows))
        impact = _impact(rec)
        assert impact["kind"] == "replica_schedule"
        assert impact["assumptions"]["removed_hours_of_day"] == list(range(8))
        # Eight of 24 observed hourly rows are removed, each at its observed allocated cost.
        assert _savings(rec) == pytest.approx(8 * 0.01 * HOURS_PER_YEAR / 24)
        assert impact["capacity_released"]["replica_hours"] == pytest.approx(8 * HOURS_PER_YEAR / 24)

    def test_idle_namespace_projects_deleting_every_workload(self):
        from greenkube.core.optimization.analyzers import IdleNamespaceAnalyzer

        rows = [
            CombinedMetric(pod_name="a", namespace="sandbox", node="n1", cpu_request=200, total_cost=0.5, joules=1.0),
            CombinedMetric(pod_name="b", namespace="sandbox", node="n1", cpu_request=300, total_cost=0.25, joules=1.0),
        ]
        (rec,) = IdleNamespaceAnalyzer(Config()).analyze(self._context(rows))
        impact = _impact(rec)
        assert impact["kind"] == "workload_deletion"
        assert _savings(rec) == pytest.approx(0.75 * HOURS_PER_YEAR / 24)
        assert impact["capacity_released"]["cpu_core_hours"] == pytest.approx(0.5 * (300 / 3600) * HOURS_PER_YEAR / 24)

    def test_orphaned_assets_keep_cost_and_leave_co2e_unprojected(self):
        from types import SimpleNamespace

        from greenkube.core.optimization.analyzers import OrphanedLBAnalyzer, OrphanedPVAnalyzer

        context = self._context(
            [],
            persistent_volumes=[SimpleNamespace(name="pv-1", phase="Released", capacity_bytes=10 * GIB)],
            load_balancers=[SimpleNamespace(name="lb-1", namespace="web", endpoint_count=0, annual_cost=120.0)],
        )
        (pv,) = OrphanedPVAnalyzer(Config()).analyze(context)
        (lb,) = OrphanedLBAnalyzer(Config()).analyze(context)
        assert _savings(pv) == pytest.approx(10 * Config().STORAGE_COST_PER_GIB_MONTH * 12)
        assert _impact(pv)["assumptions"]["cost_source"] == "custom_rate"
        assert _impact(pv)["cost_breakdown_annual"]["storage"] == pytest.approx(_savings(pv))
        assert _savings(lb) == 120.0
        assert _impact(lb)["assumptions"]["cost_source"] == "opencost"
        assert pv.potential_savings_co2e_grams is None
        assert lb.potential_savings_co2e_grams is None
