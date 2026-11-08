"""The Impact Model allocates the rows collection stores (roadmap B2, B5).

Drives the real :class:`HistoricalRangeProcessor` with a fake Prometheus and
the real SQLite repository: stored pod rows carry the reservation-aware
allocation and ``model_version``, every node gets an ``__idle__`` row, and pod
rows plus idle rows add up to the node totals (conservation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.impact import IMPACT_MODEL_VERSION
from greenkube.impact.power import GpuPowerModel
from greenkube.impact.pricing import CustomRates
from greenkube.models.node import NodeInfo
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP_SECONDS = 300
# A past, hour-aligned window: never compared with the wall clock.
BASE = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
END = BASE + timedelta(seconds=STEP_SECONDS)
GIB = 1024**3
# m5.large: 2 vCPU, AWS CCF coefficients 0.74 W / 3.5 W per vCPU, 8 GiB.
IDLE_W, MAX_W = 1.48, 7.0


@pytest.fixture
async def setup(tmp_path):
    managers = []

    async def build(prom: FakePrometheus, opencost: FakeOpenCost, **kwargs):
        cfg = Config(
            DB_TYPE="sqlite",
            DB_PATH=str(tmp_path / f"alloc-{len(managers)}.db"),
            PROMETHEUS_QUERY_RANGE_STEP="5m",
            KUBERNETES_METRICS_API_ENABLED=False,
            CLOUD_PROVIDER="aws",
        )
        dbm = DatabaseManager(cfg)
        await dbm.connect()
        managers.append(dbm)
        repo = SQLiteCombinedMetricsRepository(dbm)
        nodes = {
            "n1": NodeInfo(
                name="n1",
                instance_type="m5.large",
                cloud_provider="aws",
                cpu_capacity_cores=2,
                memory_capacity_bytes=8 * GIB,
            )
        }
        processor = build_range_processor(
            cfg,
            repo,
            prom,
            opencost,
            pods=(("ns", "a", "app"), ("ns", "b", "app")),
            nodes=nodes,
            **kwargs,
        )
        return processor, repo

    yield build
    for manager in managers:
        await manager.close()


def _cluster(prom: FakePrometheus) -> None:
    for pod, cpu, memory in (("a", 0.5, 2 * GIB), ("b", 0.1, 1 * GIB)):
        labels = {"namespace": "ns", "pod": pod, "node": "n1", "container": "app"}
        prom.add("container_cpu_usage_seconds_total", labels, cpu)
        prom.add("container_memory_working_set_bytes", labels, memory)


def _dcgm(prom: FakePrometheus) -> None:
    # GPU 0 runs pod a. Prometheus scraped the exporter without honorLabels, so
    # the workload labels are exported_*; GPU 1 is idle (no pod).
    exporter = {"namespace": "gpu-operator", "pod": "nvidia-dcgm-exporter-x", "Hostname": "n1", "modelName": "Tesla T4"}
    gpu0 = {**exporter, "gpu": "0", "UUID": "GPU-0", "exported_namespace": "ns", "exported_pod": "a"}
    gpu1 = {**exporter, "gpu": "1", "UUID": "GPU-1"}
    prom.add("DCGM_FI_DEV_POWER_USAGE", gpu0, 60.0)
    prom.add("DCGM_FI_DEV_GPU_UTIL", gpu0, 80.0)
    prom.add("DCGM_FI_DEV_GPU_UTIL", gpu1, 0.0)  # no power series: TDP idle floor


def _node_watts(cpu_used: float, memory_gib: float = 8.0, gpu_w: float = 0.0) -> float:
    return IDLE_W + cpu_used / 2 * (MAX_W - IDLE_W) + memory_gib * 0.392 + gpu_w


@pytest.mark.asyncio
async def test_rows_carry_reservation_aware_allocation_and_idle_rows_conserve_node_energy(setup):
    prom = FakePrometheus()
    _cluster(prom)
    processor, repo = await setup(prom, FakeOpenCost(pods=()))

    rows = await processor.run_range(BASE, END)

    pods = {row.pod_name: row for row in rows if not row.container_name}
    assert set(pods) == {"a", "b"}
    assert all(row.model_version == IMPACT_MODEL_VERSION for row in rows)
    (idle,) = processor.last_idle_metrics
    assert idle.node == "n1" and idle.scope == "idle"

    # Both pods request 1 core (harness): each holds half the idle power, the
    # node is fully reserved, so __idle__ gets no CPU idle power.
    a, b = pods["a"], pods["b"]
    dynamic = 0.6 / 2 * (MAX_W - IDLE_W)
    assert a.joules == pytest.approx((IDLE_W / 2 + dynamic * 0.5 / 0.6 + 2 * 0.392) * STEP_SECONDS)
    assert b.joules == pytest.approx((IDLE_W / 2 + dynamic * 0.1 / 0.6 + 1 * 0.392) * STEP_SECONDS)
    # Unreserved memory (8 - 3 GiB) powers __idle__.
    assert idle.joules == pytest.approx(5 * 0.392 * STEP_SECONDS)
    assert idle.cpu_unreserved_cores == pytest.approx(0.0)
    assert idle.memory_unreserved_bytes == pytest.approx(5 * GIB)

    # Conservation: Σ pods + __idle__ = node totals (energy, operational and embodied CO2e, cost).
    total_joules = sum(row.joules for row in pods.values()) + idle.joules
    assert total_joules == pytest.approx(_node_watts(0.6) * STEP_SECONDS)
    intensity = a.grid_intensity * a.pue
    total_co2e = sum(row.co2e_grams for row in pods.values()) + idle.co2e_grams
    assert total_co2e == pytest.approx(total_joules / 3.6e6 * intensity)
    total_embodied = sum(row.embodied_co2e_grams or 0 for row in pods.values()) + idle.embodied_co2e_grams
    assert total_embodied == pytest.approx(100 * 1000 / 35040 * STEP_SECONDS / 3600)
    prices = CustomRates.from_config(Config()).unit_prices()
    node_cost = (2 * prices.cpu_core_hour + 8 * prices.ram_gib_hour) * STEP_SECONDS / 3600
    # No catalog price without a region: the custom (OpenCost default) rates price the node.
    assert a.cost_source == "custom_rate"
    assert sum(row.total_cost for row in pods.values()) + idle.total_cost == pytest.approx(node_cost)

    # Without OpenCost the cost is modeled and labeled, never "opencost" (BUG-17).
    for row in pods.values():
        assert row.cost_source in {"pricing_catalog", "custom_rate"}
        assert row.total_cost > 0
        assert row.total_cost == pytest.approx((row.cost_cpu or 0) + (row.cost_ram or 0))

    # run_range only returns the idle rows; the collection job commits them
    # after its leader-fence check (A14), next to the pod rows.
    assert await repo.read_idle_metrics(BASE, END) == []
    await repo.write_idle_metrics(processor.last_idle_metrics)
    (stored,) = await repo.read_idle_metrics(BASE, END)
    assert stored.joules == pytest.approx(idle.joules)
    assert stored.model_version == IMPACT_MODEL_VERSION


@pytest.mark.asyncio
async def test_observed_opencost_costs_are_kept(setup):
    prom = FakePrometheus()
    _cluster(prom)
    processor, _repo = await setup(prom, FakeOpenCost(pods=(("ns", "a"),)))

    rows = await processor.run_range(BASE, END)

    pods = {row.pod_name: row for row in rows if not row.container_name}
    # FakeOpenCost prices a window by its minute-of-hour / 100: 0.0 at :00, so use the label.
    assert pods["a"].cost_source == "opencost"
    assert pods["b"].cost_source in {"pricing_catalog", "custom_rate"}


@pytest.mark.asyncio
async def test_dcgm_gpu_power_is_attributed_to_pods_and_idle(setup):
    """Acceptance (B5): GPU nodes get non-zero power when DCGM is present; gpu_usage is filled."""
    prom = FakePrometheus()
    _cluster(prom)
    _dcgm(prom)
    processor, _repo = await setup(prom, FakeOpenCost(pods=()))

    rows = await processor.run_range(BASE, END)

    pods = {row.pod_name: row for row in rows if not row.container_name}
    containers = {row.pod_name: row for row in rows if row.container_name}
    (idle,) = processor.last_idle_metrics
    t4 = GpuPowerModel().profile_for("Tesla T4")
    assert t4 is not None
    assert pods["a"].gpu_power_watts == pytest.approx(60.0)
    assert pods["a"].gpu_usage_millicores == 800
    assert pods["b"].gpu_power_watts is None
    assert containers["a"].gpu_power_watts == pytest.approx(60.0)
    # The idle GPU draws the T4 idle floor and is charged to __idle__.
    assert idle.gpu_power_watts == pytest.approx(t4.idle_watts)
    total = sum(row.joules for row in pods.values()) + idle.joules
    assert total == pytest.approx(_node_watts(0.6, gpu_w=60.0 + t4.idle_watts) * STEP_SECONDS)


@pytest.mark.asyncio
async def test_namespace_filtered_runs_expose_but_do_not_write_idle_rows(setup):
    prom = FakePrometheus()
    _cluster(prom)
    processor, repo = await setup(prom, FakeOpenCost(pods=()))

    await processor.run_range(BASE, END, namespace="ns")

    assert len(processor.last_idle_metrics) == 1
    assert await repo.read_idle_metrics(BASE, END) == []
