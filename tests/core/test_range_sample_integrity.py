"""Per-sample integrity of the range pipeline: A5 (BUG-05), A6 (BUG-09) and A10 (BUG-08)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.metrics_server_collector import MetricsServerCollector
from greenkube.core import collection_telemetry
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.energy.node_power import (
    POWER_SOURCE_DEFAULT_PROFILE,
    POWER_SOURCE_INSTANCE_PROFILE,
    POWER_SOURCE_NODE_CAPACITY_DERIVED,
    resolve_node_power_profile,
)
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import ContainerResourceUsage
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = timedelta(minutes=5)
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
POD_LABELS = {"namespace": "ns", "pod": "p", "node": "n1"}


@pytest.fixture
async def make_processor(tmp_path):
    managers = []

    async def build(prom: FakePrometheus, *, metrics_api: bool = False, **kwargs):
        cfg = Config(
            DB_TYPE="sqlite",
            DB_PATH=str(tmp_path / f"integrity-{len(managers)}.db"),
            PROMETHEUS_QUERY_RANGE_STEP="5m",
            KUBERNETES_METRICS_API_ENABLED=metrics_api,
            CLOUD_PROVIDER="aws",
        )
        dbm = DatabaseManager(cfg)
        await dbm.connect()
        managers.append(dbm)
        return build_range_processor(cfg, SQLiteCombinedMetricsRepository(dbm), prom, FakeOpenCost(), **kwargs)

    yield build
    for manager in managers:
        await manager.close()


def _dropped(reason: str) -> float:
    return collection_telemetry.DROPPED_SAMPLES.labels(reason=reason)._value.get()


# ---------------------------------------------------------------------------
# A5 — metrics-server usage attaches to the latest fresh sample only
# ---------------------------------------------------------------------------


class _MetricsServer(MetricsServerCollector):
    def __init__(self, measured_at: datetime) -> None:
        super().__init__()
        self.measured_at = measured_at

    async def collect(self):
        return [
            ContainerResourceUsage(
                namespace="ns",
                pod_name="p",
                container_name="app",
                cpu_usage_millicores=150,
                memory_usage_bytes=64 * 1024**2,
                timestamp=self.measured_at,
                duration_seconds=15,
            )
        ]


def _current_three_step_window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, tz=timezone.utc)
    return end - 3 * STEP, end


@pytest.mark.asyncio
async def test_metrics_server_usage_fills_only_the_latest_sample_and_keeps_its_timestamp(make_processor):
    prom = FakePrometheus()
    # cAdvisor without container labels: only the pod cgroup series exists.
    prom.add("container_cpu_usage_seconds_total", POD_LABELS, 0.2)
    processor = await make_processor(prom, metrics_api=True)
    start, end = _current_three_step_window()
    processor._metrics_server_collector = _MetricsServer(datetime.now(timezone.utc))

    rows = await processor.run_range(start, end)

    pods = sorted((r for r in rows if not r.container_name), key=lambda r: r.timestamp)
    containers = [r for r in rows if r.container_name]
    assert [r.timestamp for r in pods] == [start, start + STEP, start + 2 * STEP]
    assert len(containers) == 1
    latest = containers[0]
    assert latest.timestamp == start + 2 * STEP
    assert latest.duration_seconds == 300
    assert latest.cpu_usage_millicores == 150


@pytest.mark.asyncio
async def test_stale_metrics_server_usage_is_never_attached(make_processor):
    prom = FakePrometheus()
    prom.add("container_cpu_usage_seconds_total", POD_LABELS, 0.2)
    processor = await make_processor(prom, metrics_api=True)
    start, end = _current_three_step_window()
    processor._metrics_server_collector = _MetricsServer(datetime.now(timezone.utc) - timedelta(minutes=30))

    rows = await processor.run_range(start, end)

    assert [r for r in rows if r.container_name] == []


@pytest.mark.asyncio
async def test_historical_ranges_never_query_the_metrics_server(make_processor):
    prom = FakePrometheus()
    prom.add("container_cpu_usage_seconds_total", {**POD_LABELS, "container": "app"}, 0.2)
    processor = await make_processor(prom, metrics_api=True)

    class _Exploding(MetricsServerCollector):
        async def collect(self):
            raise AssertionError("point-in-time usage must not back-fill history")

    processor._metrics_server_collector = _Exploding()

    rows = await processor.run_range(BASE, BASE + 3 * STEP)

    assert len([r for r in rows if not r.container_name]) == 3


# ---------------------------------------------------------------------------
# A6 — NaN, Inf and negative values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_finite_samples_are_dropped_and_negative_rates_clamped(make_processor):
    prom = FakePrometheus()
    values = {1: float("nan"), 2: float("inf"), 3: -0.5, 4: 0.3}

    def cpu(ts: float) -> float:
        return values[int((ts - BASE.timestamp()) // 300)]

    prom.add("container_cpu_usage_seconds_total", {**POD_LABELS, "container": "app"}, cpu)
    prom.add("container_memory_working_set_bytes", {**POD_LABELS, "container": "app"}, float("nan"))
    processor = await make_processor(prom)
    non_finite_before = _dropped(collection_telemetry.DROP_REASON_NON_FINITE)
    negative_before = _dropped(collection_telemetry.DROP_REASON_NEGATIVE)

    rows = await processor.run_range(BASE, BASE + 4 * STEP)

    pods = sorted((r for r in rows if not r.container_name), key=lambda r: r.timestamp)
    # [10:00) and [10:05) were NaN/Inf and are dropped; [10:10) negative -> 0; [10:15) is valid.
    assert [(r.timestamp, r.cpu_usage_millicores) for r in pods] == [(BASE + 2 * STEP, 0), (BASE + 3 * STEP, 300)]
    assert all(r.memory_usage_bytes is None for r in pods)
    assert _dropped(collection_telemetry.DROP_REASON_NON_FINITE) == non_finite_before + 2 + 4
    assert _dropped(collection_telemetry.DROP_REASON_NEGATIVE) == negative_before + 1


# ---------------------------------------------------------------------------
# A10 — unknown node power profile
# ---------------------------------------------------------------------------


def test_power_profile_resolution_order_and_provenance():
    profiles = {"m5.large": {"vcores": 2, "minWatts": 1.48, "maxWatts": 7.0}}
    default = {"vcores": 1, "minWatts": 1.0, "maxWatts": 10.0}

    known = resolve_node_power_profile(
        node_name="a",
        instance_type="m5.large",
        cpu_capacity_cores=2,
        provider="aws",
        instance_profiles=profiles,
        default_profile=default,
    )
    derived = resolve_node_power_profile(
        node_name="b",
        instance_type="custom.metal",
        cpu_capacity_cores=8,
        provider="aws",
        instance_profiles=profiles,
        default_profile=default,
    )
    inferred = resolve_node_power_profile(
        node_name="c",
        instance_type="cpu-4",
        cpu_capacity_cores=None,
        provider=None,
        instance_profiles=profiles,
        default_profile=default,
    )
    unknown = resolve_node_power_profile(
        node_name="",
        instance_type=None,
        cpu_capacity_cores=None,
        provider=None,
        instance_profiles=profiles,
        default_profile=default,
    )

    assert known.source == POWER_SOURCE_INSTANCE_PROFILE and not known.reasons
    assert derived.source == POWER_SOURCE_NODE_CAPACITY_DERIVED
    assert derived.profile == pytest.approx({"vcores": 8, "minWatts": 8 * 0.74, "maxWatts": 8 * 3.5})
    assert "custom.metal" in derived.reasons[0]
    assert inferred.source == POWER_SOURCE_NODE_CAPACITY_DERIVED
    assert inferred.profile["vcores"] == 4
    # Cross-provider CCF average when the provider is unknown.
    assert 0.6 < inferred.profile["minWatts"] / 4 < 0.8
    assert unknown.source == POWER_SOURCE_DEFAULT_PROFILE and unknown.is_fallback


@pytest.mark.asyncio
async def test_unknown_instance_type_derives_power_from_node_capacity(make_processor):
    prom = FakePrometheus()
    prom.add("container_cpu_usage_seconds_total", {**POD_LABELS, "container": "app"}, 1.0)
    nodes = {"n1": NodeInfo(name="n1", instance_type="on-prem-box", cloud_provider="aws", cpu_capacity_cores=8)}
    processor = await make_processor(prom, nodes=nodes)

    rows = await processor.run_range(BASE, BASE + STEP)

    pod = next(r for r in rows if not r.container_name)
    # 8 vCPU x AWS CCF (0.74 / 3.5 W): util 1/8 -> 5.92 + 0.125 * (28 - 5.92) = 8.68 W.
    assert pod.joules == pytest.approx(8.68 * 300)
    assert pod.is_estimated
    assert any("derived from 8 vCPU capacity" in reason for reason in pod.estimation_reasons)
    assert collection_telemetry.UNKNOWN_POWER_PROFILE_NODES._value.get() == 0


@pytest.mark.asyncio
async def test_missing_node_label_is_resolved_from_kube_pod_info(make_processor):
    prom = FakePrometheus()
    # cAdvisor relabelling dropped `node`; kube-state-metrics knows the placement.
    prom.add("container_cpu_usage_seconds_total", {"namespace": "ns", "pod": "p", "container": "app"}, 0.2)
    prom.add("kube_pod_info", {"namespace": "ns", "pod": "p", "node": "n1"}, 1)
    processor = await make_processor(prom)

    rows = await processor.run_range(BASE, BASE + STEP)

    pod = next(r for r in rows if not r.container_name)
    assert pod.node == "n1"
    assert pod.joules == pytest.approx(609.6)


@pytest.mark.asyncio
async def test_unresolvable_node_uses_default_profile_and_is_reported(make_processor):
    prom = FakePrometheus()
    prom.add("container_cpu_usage_seconds_total", {"namespace": "ns", "pod": "p", "container": "app"}, 0.2)
    processor = await make_processor(prom, nodes={}, instance_types={})

    rows = await processor.run_range(BASE, BASE + STEP)

    pod = next(r for r in rows if not r.container_name)
    assert pod.node is None
    assert any("DEFAULT_INSTANCE_PROFILE" in reason for reason in pod.estimation_reasons)
    assert collection_telemetry.UNKNOWN_POWER_PROFILE_NODES._value.get() == 1
