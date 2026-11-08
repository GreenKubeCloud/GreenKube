# tests/impact/test_power.py
"""Memory and GPU power models (roadmap B5) and their allocation."""

import random

import pytest

from greenkube.impact import (
    IDLE_WORKLOAD_NAME,
    CostSource,
    NodeImpactProfile,
    ResourceSample,
    UnitPrices,
    WorkloadRef,
    allocate_node,
    node_totals,
)
from greenkube.impact.power import (
    DEFAULT_MEMORY_WATTS_PER_GIB,
    GPU_SOURCE_DCGM,
    GPU_SOURCE_TDP,
    GpuDevice,
    GpuPowerModel,
    LinearCpuPowerModel,
    MemoryPowerModel,
    memory_watts_per_gib,
    node_gpu_power,
)
from greenkube.models import provenance


def _m5_large(memory_w_per_gib=DEFAULT_MEMORY_WATTS_PER_GIB, gpu_unattributed=0.0):
    # m5.large: 2 vCPU, 8 GiB.
    return NodeImpactProfile(
        name="m5",
        cpu_capacity_cores=2,
        mem_capacity_gib=8,
        idle_watts=3.23,
        max_watts=36.3,
        embodied_g_per_hour=0.0,
        prices=UnitPrices(0.04, 0.005, "USD", CostSource.PRICING_CATALOG, "fixture"),
        pue=1.0,
        grid_intensity_g_per_kwh=100.0,
        memory_watts_per_gib=memory_w_per_gib,
        gpu_unattributed_watts=gpu_unattributed,
    )


def _sample(pod, cpu_req, mem_req, cpu_use=None, mem_use=None, gpu_watts=None, hours=1.0):
    return ResourceSample(
        ref=WorkloadRef("c", "ns", "Pod", pod),
        hours=hours,
        cpu_request_cores=cpu_req,
        cpu_usage_cores=cpu_use,
        mem_request_gib=mem_req,
        mem_usage_gib=mem_use,
        node="m5",
        gpu_watts=gpu_watts,
    )


class TestMemoryPower:
    def test_default_is_the_ccf_coefficient(self):
        assert MemoryPowerModel().watts(1) == pytest.approx(0.392)
        assert memory_watts_per_gib(object()) == DEFAULT_MEMORY_WATTS_PER_GIB

    def test_m5_large_fully_reserved_adds_about_three_watts(self):
        """Acceptance: on the m5.large fixture, memory adds ≈3 W at 8 GiB fully reserved."""
        impacts = allocate_node(_m5_large(), [_sample("p", 0.0, 8.0)], 1.0)
        pod = next(impact for impact in impacts if impact.scope == "workload")
        idle = next(impact for impact in impacts if impact.scope == "idle")
        assert pod.energy_kwh.dynamic_mem * 1000 == pytest.approx(3.136)
        assert idle.energy_kwh.dynamic_mem == 0.0

    def test_unreserved_memory_power_goes_to_idle(self):
        impacts = allocate_node(_m5_large(), [_sample("p", 0.0, 2.0)], 1.0)
        idle = next(impact for impact in impacts if impact.ref.name == IDLE_WORKLOAD_NAME)
        assert idle.energy_kwh.dynamic_mem * 1000 == pytest.approx(6 * 0.392)

    def test_memory_overcommit_keeps_conservation(self):
        samples = [_sample("a", 0.5, 4.0, mem_use=6.0), _sample("b", 0.5, 4.0)]
        impacts = allocate_node(_m5_large(), samples, 1.0)
        totals = node_totals(_m5_large(), samples, 1.0)
        assert totals.memory_energy_kwh * 1000 == pytest.approx(10 * 0.392)
        assert sum(i.energy_kwh.dynamic_mem for i in impacts) == pytest.approx(totals.memory_energy_kwh)

    def test_linear_cpu_model(self):
        model = LinearCpuPowerModel(idle_watts=10, max_watts=30)
        assert model.watts(0) == 10
        assert model.watts(0.5) == 20
        assert model.watts(2) == 30


class TestGpuPower:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("NVIDIA A100-SXM4-40GB", "A100 SXM"),
            ("NVIDIA-A100-PCIE-40GB", "A100 PCIe 40GB"),
            ("NVIDIA A100 80GB PCIe", "A100 PCIe 80GB"),
            ("NVIDIA H100 80GB HBM3", "H100 SXM"),
            ("NVIDIA H100 PCIe", "H100 PCIe"),
            ("NVIDIA A10G", "A10G"),
            ("NVIDIA A10", "A10"),
            ("Tesla T4", "T4"),
            ("NVIDIA L4", "L4"),
            ("NVIDIA L40S", "L40S"),
            ("Tesla V100-SXM2-16GB", "V100 SXM2"),
        ],
    )
    def test_profile_matching(self, name, expected):
        profile = GpuPowerModel().profile_for(name)
        assert profile is not None
        assert profile.name == expected

    def test_unknown_gpu_has_no_profile(self):
        assert GpuPowerModel().profile_for("Radeon Instinct MI250") is None
        assert GpuPowerModel().profile_for(None) is None

    def test_dcgm_power_wins_over_tdp(self):
        device = GpuDevice(node="g", device="0", model_name="Tesla T4", power_watts=33.0, utilization_pct=50)
        assert GpuPowerModel().device_watts(device) == (33.0, GPU_SOURCE_DCGM)

    def test_tdp_model_without_power(self):
        model = GpuPowerModel()
        t4 = model.profile_for("Tesla T4")
        assert t4 is not None
        full = model.device_watts(GpuDevice(node="g", device="0", model_name="Tesla T4", utilization_pct=100))
        assert full is not None
        watts, source = full
        assert source == GPU_SOURCE_TDP
        assert watts == pytest.approx(t4.tdp_watts)
        floor = model.device_watts(GpuDevice(node="g", device="0", model_name="Tesla T4"))
        assert floor is not None
        idle, _ = floor
        assert idle == pytest.approx(t4.idle_watts)
        assert 0 < idle < t4.tdp_watts

    def test_unknown_device_without_power_is_skipped(self):
        assert GpuPowerModel().device_watts(GpuDevice(node="g", device="0", model_name="mystery")) is None

    def test_node_gpu_power_attributes_gpus_to_pods(self):
        devices = [
            GpuDevice(node="g", device="0", power_watts=200, utilization_pct=80, namespace="ml", pod="train"),
            GpuDevice(node="g", device="1", power_watts=180, utilization_pct=40, namespace="ml", pod="train"),
            GpuDevice(node="g", device="2", power_watts=50, utilization_pct=0),
        ]
        power = node_gpu_power(devices)
        assert power.by_pod == {("ml", "train"): 380}
        assert power.usage_by_pod == {("ml", "train"): 1200}
        assert power.unattributed_watts == 50
        assert power.total_watts == 430
        assert power.sources == (GPU_SOURCE_DCGM,)

    def test_gpu_nodes_get_power_when_dcgm_is_present(self):
        """Acceptance: GPU nodes get non-zero power when DCGM is present."""
        node = _m5_large(gpu_unattributed=50.0)
        impacts = allocate_node(node, [_sample("train", 1.0, 4.0, cpu_use=1.0, gpu_watts=380.0)], 1.0)
        pod = next(i for i in impacts if i.scope == "workload")
        idle = next(i for i in impacts if i.scope == "idle")
        assert pod.energy_kwh.gpu == pytest.approx(0.38)
        assert idle.energy_kwh.gpu == pytest.approx(0.05)
        assert node_totals(node, [_sample("train", 1.0, 4.0, gpu_watts=380.0)], 1.0).gpu_energy_kwh == pytest.approx(
            0.43
        )


class TestConservationWithMemoryAndGpu:
    @pytest.mark.parametrize("seed", range(20))
    def test_allocated_plus_idle_equals_node_totals(self, seed):
        rng = random.Random(seed)
        node = NodeImpactProfile(
            name="n",
            cpu_capacity_cores=rng.choice([2, 4, 16]),
            mem_capacity_gib=rng.choice([0, 8, 64]),
            idle_watts=rng.uniform(5, 100),
            max_watts=rng.uniform(100, 300),
            embodied_g_per_hour=rng.uniform(1, 20),
            prices=UnitPrices(0.03, 0.004, "USD", CostSource.CUSTOM_RATE, "configured"),
            pue=rng.uniform(1.0, 1.5),
            grid_intensity_g_per_kwh=rng.uniform(10, 600),
            memory_watts_per_gib=rng.choice([0.0, 0.392, 1.0]),
            gpu_unattributed_watts=rng.choice([0.0, 70.0]),
        )
        hours = rng.choice([1 / 12, 1.0])
        samples = [
            ResourceSample(
                ref=WorkloadRef("c", "ns", "Pod", f"p{index}"),
                hours=hours * rng.choice([0.5, 1.0]),
                cpu_request_cores=rng.uniform(0, 4),
                cpu_usage_cores=rng.choice([None, rng.uniform(0, 4)]),
                mem_request_gib=rng.uniform(0, 32),
                mem_usage_gib=rng.choice([None, rng.uniform(0, 32)]),
                node="n",
                gpu_watts=rng.choice([None, rng.uniform(0, 400)]),
            )
            for index in range(rng.randint(0, 10))
        ]
        impacts = allocate_node(node, samples, hours)
        totals = node_totals(node, samples, hours)
        assert sum(i.energy_kwh.total for i in impacts) == pytest.approx(totals.energy_kwh, rel=1e-9, abs=1e-12)
        assert sum(i.co2e_operational_g for i in impacts) == pytest.approx(
            totals.co2e_operational_g, rel=1e-9, abs=1e-9
        )
        assert sum(i.co2e_embodied_g for i in impacts) == pytest.approx(totals.co2e_embodied_g, rel=1e-9, abs=1e-12)


class TestUnifiedEnums:
    def test_one_cost_source_and_one_power_source(self):
        from greenkube.impact import CostSource as ImpactCostSource
        from greenkube.impact import PowerSource as ImpactPowerSource
        from greenkube.impact.types import parse_cost_source

        assert ImpactCostSource is provenance.CostSource
        assert ImpactPowerSource is provenance.PowerSource
        assert parse_cost_source("mixed") == (provenance.CostSource.OPENCOST, "mixed")
        assert parse_cost_source("pricing_catalog") == (provenance.CostSource.PRICING_CATALOG, None)
