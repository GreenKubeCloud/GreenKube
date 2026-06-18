from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from greenkube.models.metrics import CombinedMetric
from greenkube.storage.base_repository import CombinedMetricsRepository


def _metric(
    pod_name: str,
    namespace: str,
    timestamp: datetime,
    co2e_grams: float,
    total_cost: float = 1.0,
    joules: float = 100.0,
) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod_name,
        namespace=namespace,
        timestamp=timestamp,
        co2e_grams=co2e_grams,
        embodied_co2e_grams=co2e_grams / 10,
        total_cost=total_cost,
        joules=joules,
        cpu_usage_millicores=100,
        memory_usage_bytes=256,
    )


class DummyCombinedMetricsRepository(CombinedMetricsRepository):
    def __init__(self, raw_metrics=None, hourly_metrics=None):
        self.raw_metrics = raw_metrics or []
        self.hourly_metrics = hourly_metrics or []
        self.written = []
        self.read_calls = []
        self.hourly_calls = []

    async def write_combined_metrics(self, metrics):
        self.written.extend(metrics)
        return len(metrics)

    async def read_combined_metrics(self, start_time, end_time):
        self.read_calls.append((start_time, end_time))
        return self.raw_metrics

    async def read_hourly_metrics(self, start_time, end_time, namespace=None):
        self.hourly_calls.append((start_time, end_time, namespace))
        metrics = self.hourly_metrics
        if namespace:
            metrics = [metric for metric in metrics if metric.namespace == namespace]
        return metrics


@pytest.mark.asyncio
async def test_read_hourly_metrics_default_filters_namespace():
    now = datetime.now(timezone.utc)
    repo = DummyCombinedMetricsRepository(
        raw_metrics=[_metric("api", "prod", now, 2.0), _metric("worker", "dev", now, 3.0)]
    )

    metrics = await CombinedMetricsRepository.read_hourly_metrics(repo, now - timedelta(hours=1), now, namespace="prod")

    assert [metric.pod_name for metric in metrics] == ["api"]


@pytest.mark.asyncio
async def test_read_combined_metrics_smart_uses_raw_hourly_and_mixed_paths():
    now = datetime.now(timezone.utc)
    raw = [_metric("raw-api", "prod", now - timedelta(minutes=10), 2.0)]
    hourly = [_metric("hourly-api", "prod", now - timedelta(days=2), 5.0)]
    repo = DummyCombinedMetricsRepository(raw_metrics=raw, hourly_metrics=hourly)

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24)):
        recent = await repo.read_combined_metrics_smart(now - timedelta(hours=2), now, namespace="prod")
        old = await repo.read_combined_metrics_smart(now - timedelta(days=3), now - timedelta(days=2), namespace="prod")
        mixed = await repo.read_combined_metrics_smart(now - timedelta(days=3), now, namespace="prod")

    assert recent == raw
    assert old == hourly
    assert mixed == hourly + raw
    assert len(repo.read_calls) == 2
    assert len(repo.hourly_calls) == 2


@pytest.mark.asyncio
async def test_default_list_namespaces_and_aggregate_summary():
    now = datetime.now(timezone.utc)
    repo = DummyCombinedMetricsRepository(
        raw_metrics=[
            _metric("api-a", "prod", now, 10.0, total_cost=1.5, joules=100.0),
            _metric("api-b", "prod", now, 5.0, total_cost=2.5, joules=200.0),
            _metric("worker", "dev", now, 2.0, total_cost=3.0, joules=300.0),
        ]
    )

    assert await repo.list_namespaces() == ["dev", "prod"]

    summary = await repo.aggregate_summary(now - timedelta(hours=1), now, namespace="prod")

    assert summary == {
        "total_co2e_grams": 15.0,
        "total_embodied_co2e_grams": 1.5,
        "total_cost": 4.0,
        "total_energy_joules": 300.0,
        "pod_count": 2,
        "namespace_count": 1,
        "row_count": 2,
    }


@pytest.mark.asyncio
async def test_default_aggregate_timeseries_groups_and_sorts_buckets():
    first = datetime(2026, 4, 30, 10, 10, tzinfo=timezone.utc)
    second = datetime(2026, 4, 30, 11, 10, tzinfo=timezone.utc)
    repo = DummyCombinedMetricsRepository(
        raw_metrics=[
            _metric("api-a", "prod", second, 3.0, total_cost=2.0, joules=30.0),
            _metric("api-b", "prod", first, 2.0, total_cost=1.0, joules=20.0),
            _metric("ignored", "dev", first, 99.0),
        ]
    )

    points = await repo.aggregate_timeseries(first - timedelta(hours=1), second + timedelta(hours=1), namespace="prod")

    assert [point["timestamp"] for point in points] == ["2026-04-30T10:00:00Z", "2026-04-30T11:00:00Z"]
    assert [point["co2e_grams"] for point in points] == [2.0, 3.0]
    assert points[0]["memory_usage_bytes"] == 256


@pytest.mark.asyncio
async def test_default_namespace_and_top_pod_aggregations_use_smart_reads():
    now = datetime.now(timezone.utc)
    repo = DummyCombinedMetricsRepository(
        raw_metrics=[
            _metric("small", "prod", now, 1.0),
            _metric("large", "prod", now, 10.0),
            _metric("dev-api", "dev", now, 7.0),
        ]
    )

    by_namespace = await repo.aggregate_by_namespace(now - timedelta(hours=1), now)
    top_pods = await repo.aggregate_top_pods(now - timedelta(hours=1), now, namespace="prod", limit=1)

    assert [row["namespace"] for row in by_namespace] == ["prod", "dev"]
    assert by_namespace[0]["co2e_grams"] == 11.0
    assert top_pods == [
        {
            "namespace": "prod",
            "pod_name": "large",
            "co2e_grams": 10.0,
            "embodied_co2e_grams": 1.0,
            "total_cost": 1.0,
            "energy_joules": 100.0,
        }
    ]


# ---------------------------------------------------------------------------
# read_combined_metrics_page — Python fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_combined_metrics_page_returns_total_and_page():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)
    metrics = [_metric(f"pod-{i}", "prod", future, float(i)) for i in range(5)]
    repo = DummyCombinedMetricsRepository(raw_metrics=metrics)

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24)):
        total, page = await repo.read_combined_metrics_page(
            future - timedelta(hours=1), future + timedelta(hours=1), offset=1, limit=2
        )

    assert total == 5
    assert len(page) == 2
    assert page[0].pod_name == "pod-1"
    assert page[1].pod_name == "pod-2"


@pytest.mark.asyncio
async def test_read_combined_metrics_page_with_namespace_filter():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)
    metrics = [
        _metric("pod-a", "prod", future, 1.0),
        _metric("pod-b", "dev", future, 2.0),
        _metric("pod-c", "prod", future, 3.0),
    ]
    repo = DummyCombinedMetricsRepository(raw_metrics=metrics)

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24)):
        total, page = await repo.read_combined_metrics_page(
            future - timedelta(hours=1), future + timedelta(hours=1), namespace="prod", offset=0, limit=10
        )

    assert total == 2
    assert all(m.namespace == "prod" for m in page)


@pytest.mark.asyncio
async def test_read_combined_metrics_page_offset_beyond_total_returns_empty_page():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)
    repo = DummyCombinedMetricsRepository(raw_metrics=[_metric("pod-a", "prod", future, 1.0)])

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24)):
        total, page = await repo.read_combined_metrics_page(
            future - timedelta(hours=1), future + timedelta(hours=1), offset=99, limit=10
        )

    assert total == 1
    assert page == []


# ---------------------------------------------------------------------------
# read_latest_per_pod — Python fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_latest_per_pod_returns_newest_per_pod():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)
    older = future - timedelta(minutes=5)
    metrics = [
        _metric("api", "prod", older, 1.0),
        _metric("api", "prod", future, 2.0),  # latest for this pod
        _metric("worker", "prod", older, 3.0),
    ]
    repo = DummyCombinedMetricsRepository(raw_metrics=metrics)

    result = await repo.read_latest_per_pod(future - timedelta(hours=1), future + timedelta(hours=1))

    result_by_pod = {m.pod_name: m for m in result}
    assert result_by_pod["api"].co2e_grams == 2.0
    assert result_by_pod["worker"].co2e_grams == 3.0
    assert len(result) == 2


@pytest.mark.asyncio
async def test_read_latest_per_pod_handles_cross_namespace():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)
    metrics = [
        _metric("api", "prod", future, 1.0),
        _metric("api", "dev", future, 2.0),  # same pod name, different namespace → separate
    ]
    repo = DummyCombinedMetricsRepository(raw_metrics=metrics)

    result = await repo.read_latest_per_pod(future - timedelta(hours=1), future + timedelta(hours=1))

    assert len(result) == 2


# ---------------------------------------------------------------------------
# aggregate_grouped_row_count — Python fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_grouped_row_count_no_granularity_counts_distinct_pods():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)  # future timestamp always in raw path
    metrics = [
        _metric("api", "prod", future, 1.0),
        _metric("api", "prod", future - timedelta(minutes=5), 1.0),  # same pod → one group
        _metric("worker", "prod", future, 2.0),
    ]
    repo = DummyCombinedMetricsRepository(raw_metrics=metrics)

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24)):
        count = await repo.aggregate_grouped_row_count(
            future - timedelta(hours=1), future + timedelta(hours=1), group_by="pod"
        )

    assert count == 2


@pytest.mark.asyncio
async def test_aggregate_grouped_row_count_daily_granularity_counts_pod_day_combos():
    # Use future dates so they always hit the raw path (start_time > now - compression_age).
    now = datetime.now(timezone.utc)
    day1 = now + timedelta(days=10)
    day2 = now + timedelta(days=11)
    metrics = [
        _metric("api", "prod", day1, 1.0),
        _metric("api", "prod", day2, 2.0),  # same pod, different day → 2 groups
        _metric("worker", "prod", day1, 3.0),
    ]
    repo = DummyCombinedMetricsRepository(raw_metrics=metrics)

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24)):
        count = await repo.aggregate_grouped_row_count(
            day1 - timedelta(hours=1), day2 + timedelta(hours=1), granularity="daily", group_by="pod"
        )

    assert count == 3  # (api, day1), (api, day2), (worker, day1)


@pytest.mark.asyncio
async def test_aggregate_grouped_row_count_namespace_grouping():
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)  # future timestamp always in raw path
    metrics = [
        _metric("api", "prod", future, 1.0),
        _metric("worker", "prod", future, 2.0),  # same namespace → 1 group with group_by=namespace
        _metric("api", "dev", future, 3.0),
    ]
    repo = DummyCombinedMetricsRepository(raw_metrics=metrics)

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24)):
        count = await repo.aggregate_grouped_row_count(
            future - timedelta(hours=1), future + timedelta(hours=1), group_by="namespace"
        )

    assert count == 2  # prod and dev
