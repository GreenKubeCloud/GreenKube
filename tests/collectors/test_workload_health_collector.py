"""Prometheus-backed workload health signals for verification (C0.9, BUG-35)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from greenkube.collectors.workload_health_collector import WorkloadHealthCollector, pod_selector
from greenkube.core.config import Config

END = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
START = END - timedelta(hours=72)


def _vector(value):
    return [{"metric": {}, "value": [END.timestamp(), str(value)]}]


class FakePrometheus:
    """Answers by metric name; ``None`` simulates a failed query, ``[]`` an empty result."""

    def __init__(self, answers):
        self.answers = answers
        self.queries: list[str] = []

    async def __call__(self, promql, at):
        self.queries.append(promql)
        for name, answer in self.answers.items():
            if name in promql:
                return answer
        return []


async def _collect(answers, **kwargs):
    prometheus = FakePrometheus(answers)
    observation = await WorkloadHealthCollector(query=prometheus).collect(
        namespace="prod",
        pods=kwargs.pop("pods", ["api-app-a", "api-app-b"]),
        container_name=kwargs.pop("container_name", "app"),
        start=START,
        end=END,
        owner_kind="Deployment",
        owner_name="api",
    )
    return observation, prometheus


async def test_all_signals_are_read_over_the_window():
    observation, prometheus = await _collect(
        {
            "count(kube_pod_status_ready": _vector(2),
            "last_terminated_reason": _vector(1),
            "avg(avg_over_time(kube_pod_status_ready": _vector(0.995),
            "cfs_throttled_periods_total": _vector(30),
            "cfs_periods_total": _vector(1000),
        }
    )

    assert observation.oom_kill_count == 1
    assert observation.readiness_ratio == pytest.approx(0.995)
    assert observation.throttling_ratio == pytest.approx(0.03)
    assert observation.pod_count == 2
    oom_query = next(query for query in prometheus.queries if "last_terminated_reason" in query)
    assert 'reason="OOMKilled"' in oom_query
    assert 'container="app"' in oom_query
    assert "[259200s]" in oom_query  # the 72h window, evaluated at its end
    assert 'pod=~"api\\\\-app\\\\-a|api\\\\-app\\\\-b"' in oom_query


async def test_no_oom_series_with_kube_state_metrics_means_zero_kills():
    observation, _ = await _collect(
        {
            "count(kube_pod_status_ready": _vector(2),
            "last_terminated_reason": [],
            "avg(avg_over_time(kube_pod_status_ready": _vector(1),
        }
    )

    assert observation.oom_kill_count == 0
    assert observation.readiness_ratio == 1.0
    # No CFS series: the container has no CPU limit, throttling is not available.
    assert observation.throttling_ratio is None


async def test_missing_kube_state_metrics_leaves_signals_not_available():
    observation, _ = await _collect({"count(kube_pod_status_ready": []})

    assert observation.oom_kill_count is None
    assert observation.readiness_ratio is None
    assert any("kube-state-metrics" in note for note in observation.notes)


async def test_failed_queries_are_not_reported_as_healthy():
    observation, _ = await _collect(
        {
            "count(kube_pod_status_ready": _vector(2),
            "last_terminated_reason": None,
            "avg(avg_over_time(kube_pod_status_ready": None,
            "cfs_throttled_periods_total": None,
        }
    )

    assert observation.oom_kill_count is None
    assert observation.readiness_ratio is None
    assert observation.throttling_ratio is None


async def test_no_pods_and_no_owner_pattern_reads_nothing():
    prometheus = FakePrometheus({})
    observation = await WorkloadHealthCollector(query=prometheus).collect(
        namespace="prod", pods=[], container_name=None, start=START, end=END
    )

    assert prometheus.queries == []
    assert observation.readiness_ratio is None


def test_selector_falls_back_to_the_owner_pattern_for_many_pods():
    pods = [f"api-app-{index:05d}" for index in range(500)]
    selector = pod_selector("prod", pods, owner_kind="StatefulSet", owner_name="db")

    assert selector == 'namespace="prod", pod=~"db-[0-9]+"'


async def test_http_query_uses_the_configured_prometheus(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "success", "data": {"result": _vector(1)}})

    settings = Config(PROMETHEUS_URL="http://prometheus:9090/", PROMETHEUS_BEARER_TOKEN="token")
    collector = WorkloadHealthCollector(settings)
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await collector._query("up", END)
    finally:
        await collector.close()

    assert result == _vector(1)
    assert seen["url"].startswith("http://prometheus:9090/api/v1/query?")
    assert f"time={END.timestamp():.3f}" in seen["url"]
    assert seen["auth"] == "Bearer token"


async def test_unconfigured_prometheus_makes_every_signal_unavailable():
    collector = WorkloadHealthCollector(Config(PROMETHEUS_URL=""))
    observation = await collector.collect(namespace="prod", pods=["api-0"], container_name=None, start=START, end=END)

    assert (observation.oom_kill_count, observation.readiness_ratio, observation.throttling_ratio) == (None, None, None)
    assert observation.notes == ["Prometheus URL is not configured or discovered"]
