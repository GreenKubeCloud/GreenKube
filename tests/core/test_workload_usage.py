"""Reference semantics of the workload usage rollup and summaries (roadmap E3)."""

from __future__ import annotations

import time
import tracemalloc
from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.workload_usage import (
    nearest_rank,
    nearest_rank_index,
    rollup_rows,
    summarize,
    summarize_rows,
    weighted_nearest_rank,
)
from greenkube.models.metrics import CombinedMetric

MIB = 1024 * 1024


def _hour() -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)


def _metric(ts: datetime, **overrides) -> CombinedMetric:
    values = {
        "pod_name": "api-7d9f8b6c5d-abcde",
        "namespace": "shop",
        "owner_kind": "Deployment",
        "owner_name": "api",
        "timestamp": ts,
        "duration_seconds": 300,
        "node": "node-a",
        "cpu_request": 500,
        "memory_request": 512 * MIB,
        "cpu_usage_millicores": 100,
        "memory_usage_bytes": 100 * MIB,
        "total_cost": 0.01,
    }
    values.update(overrides)
    return CombinedMetric(**values)


class TestPercentiles:
    def test_nearest_rank_matches_the_sql_integer_rank(self):
        for count in range(1, 400):
            for p in (50, 90, 95, 99):
                # (p * n + 99) / 100 in SQL integer arithmetic is the 1-based rank.
                assert nearest_rank_index(count, p) == (p * count + 99) // 100 - 1

    def test_nearest_rank_picks_an_observed_value(self):
        values = [float(v) for v in range(1, 101)]
        assert nearest_rank(values, 50) == 50
        assert nearest_rank(values, 95) == 95
        assert nearest_rank(values, 99) == 99
        assert nearest_rank([], 95) is None

    def test_weighted_nearest_rank_counts_weights(self):
        # 90 samples at 10, 10 samples at 100: p90 is 10, p95 is 100.
        points = [(10.0, 90), (100.0, 10)]
        assert weighted_nearest_rank(points, 90) == 10.0
        assert weighted_nearest_rank(points, 95) == 100.0


class TestRollup:
    def test_hour_percentiles_come_from_per_step_values(self):
        hour = _hour()
        usage = [50] * 11 + [600]
        rows = [_metric(hour + timedelta(minutes=5 * i), cpu_usage_millicores=u) for i, u in enumerate(usage)]
        (row,) = rollup_rows(rows)
        assert row.cpu_usage_avg == pytest.approx(sum(usage) / 12)
        assert row.cpu_usage_p50 == 50
        assert row.cpu_usage_p95 == 600
        assert row.cpu_usage_max == 600
        assert row.step_count == 12 and row.sample_count == 12 and row.pod_count == 1
        assert row.duration_seconds == 3600
        assert row.hour_of_day == hour.hour

    def test_per_step_maximum_wins_over_the_step_value(self):
        rows = [_metric(_hour(), container_name="web", cpu_usage_millicores=100, cpu_usage_max_millicores=900)]
        container = [row for row in rollup_rows([], rows) if row.container_name == "web"]
        assert container[0].cpu_usage_p99 == 900

    def test_latest_request_and_limits_of_the_hour(self):
        hour = _hour()
        rows = [
            _metric(hour, container_name="web", cpu_request=500, cpu_limit=1000),
            _metric(hour + timedelta(minutes=30), container_name="web", cpu_request=250, cpu_limit=None),
        ]
        container = [row for row in rollup_rows([], rows) if row.container_name == "web"][0]
        assert container.cpu_request == 250
        assert container.cpu_limit is None
        assert container.cpu_request_sum == 750

    def test_pods_without_a_pod_row_get_one_from_their_containers(self):
        hour = _hour()
        rows = [
            _metric(hour, container_name="web", cpu_usage_millicores=100, cpu_request=400),
            _metric(hour, container_name="sidecar", cpu_usage_millicores=20, cpu_request=100),
        ]
        by_container = {row.container_name: row for row in rollup_rows([], rows)}
        assert set(by_container) == {"", "web", "sidecar"}
        assert by_container[""].cpu_usage_avg == 120
        assert by_container[""].cpu_request == 500
        assert by_container["web"].node == "node-a"

    def test_bare_pods_and_inferred_deployments(self):
        hour = _hour()
        rows = [
            _metric(hour, pod_name="debug", owner_kind=None, owner_name=None),
            _metric(hour, pod_name="web-5c9f7d8b6c-x2k4p", owner_kind=None, owner_name=None),
        ]
        targets = {row.target for row in rollup_rows(rows)}
        assert targets == {("shop", "Pod", "debug"), ("shop", "Deployment", "web")}

    def test_replicas(self):
        hour = _hour()
        rows = []
        for step in range(12):
            ts = hour + timedelta(minutes=5 * step)
            rows.append(_metric(ts, pod_name="api-7d9f8b6c5d-aaaaa"))
            rows.append(_metric(ts, pod_name="api-7d9f8b6c5d-bbbbb"))
        snapshot = summarize_rows(rows)
        summary = snapshot.summaries[("shop", "Deployment", "api")][""]
        assert summary.replicas_avg == pytest.approx(2.0)
        assert summary.replicas_max == 2

    def test_one_hour_of_500_pods_is_rolled_up_quickly_in_bounded_memory(self):
        hour = _hour()
        pods, containers = [], []
        for pod in range(500):
            for step in range(12):
                ts = hour + timedelta(minutes=5 * step)
                pod_name = f"svc-{pod // 5}-7d9f8b6c5d-{pod:05d}"
                pods.append(_metric(ts, pod_name=pod_name, owner_name=f"svc-{pod // 5}", node=f"node-{pod % 20}"))
                containers.append(
                    _metric(ts, pod_name=pod_name, owner_name=f"svc-{pod // 5}", container_name="app", node=None)
                )
        tracemalloc.start()
        started = time.perf_counter()
        rows = rollup_rows(pods, containers)
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert len(rows) == 2 * 500  # 100 Deployments x 5 nodes x (pod level + container)
        assert elapsed < 5.0
        assert peak < 64 * MIB


class TestSummaries:
    def test_lookback_percentiles_are_nearest_rank_of_hourly_percentiles(self):
        start = _hour() - timedelta(hours=9)
        rows = []
        for hour in range(10):
            for step in range(12):
                ts = start + timedelta(hours=hour, minutes=5 * step)
                rows.append(_metric(ts, cpu_usage_millicores=100 + hour * 10 + step))
        hours = rollup_rows(rows)
        summary = summarize(hours).summaries[("shop", "Deployment", "api")][""]
        hourly_p95 = sorted(row.cpu_usage_p95 for row in hours if row.cpu_usage_p95 is not None)
        assert summary.cpu is not None
        assert summary.cpu.p95 == hourly_p95[nearest_rank_index(10, 95)]
        assert summary.cpu.max == 100 + 90 + 11
        assert summary.hours_observed == 10
        assert summary.coverage_seconds == 10 * 3600

    def test_rows_without_timestamps_have_unknown_coverage(self):
        snapshot = summarize_rows([CombinedMetric(pod_name="p", namespace="n", cpu_usage_millicores=5)])
        summary = snapshot.summaries[("n", "Pod", "p")][""]
        assert summary.coverage_seconds is None
