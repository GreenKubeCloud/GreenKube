"""OOM kills, CPU throttling and JVM hints filled in by scans (roadmap E9 remainder)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import MemoryRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.enrich import JVM_MEMORY_DECREASE_CONFIDENCE, enrich_recommendations
from greenkube.core.optimization.targeting import target_for_pod
from greenkube.core.reliability_signals import (
    ReliabilitySignals,
    apply_signals,
    attach_reliability_signals,
    container_kinds_from_pods,
    jvm_hint,
    jvm_hints_from_pods,
)
from greenkube.models.workload_usage import UsageSnapshot, UsageStats, WorkloadUsageSummary

MIB = 1024 * 1024
END = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
START = END - timedelta(days=14)
STS = ("monitoring", "StatefulSet", "prometheus-k8s")
API = ("shop", "Deployment", "api")


def _summary(target, container: str, **overrides: Any) -> WorkloadUsageSummary:
    values: Dict[str, Any] = {
        "namespace": target[0],
        "target_kind": target[1],
        "target_name": target[2],
        "container_name": container,
        "first_hour": END - timedelta(days=14),
        "last_hour": END,
        "hours_observed": 14 * 24,
        "memory": UsageStats(
            sample_count=4000, avg=100 * MIB, p50=100 * MIB, p90=110 * MIB, p95=115 * MIB, p99=120 * MIB, max=180 * MIB
        ),
        "memory_request": 512 * MIB,
        "cpu_request": 500,
    }
    values.update(overrides)
    return WorkloadUsageSummary(**values)


def _snapshot(*summaries: WorkloadUsageSummary) -> UsageSnapshot:
    snapshot = UsageSnapshot(source="rollup")
    for summary in summaries:
        snapshot.summaries.setdefault(summary.target, {})[summary.container_name] = summary
    return snapshot


def _series(value: float, **labels: str) -> Dict[str, Any]:
    return {"metric": labels, "value": [END.timestamp(), str(value)]}


class FakeQuery:
    """Answers PromQL by the first registered fragment it contains."""

    def __init__(self, answers: Dict[str, Optional[List[Dict[str, Any]]]]):
        self.answers = answers
        self.queries: List[str] = []

    async def __call__(self, promql: str, at: datetime):
        self.queries.append(promql)
        for fragment, answer in self.answers.items():
            if fragment in promql:
                return answer
        return []


def _pod(namespace: str, name: str, containers: List[Any], owner: Optional[tuple] = None) -> Any:
    refs = [SimpleNamespace(kind=owner[0], name=owner[1], controller=True)] if owner else []
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace, name=name, owner_references=refs),
        spec=SimpleNamespace(containers=containers),
    )


def _container(name: str, image: str, env: tuple = (), command: tuple = (), args: tuple = ()) -> Any:
    return SimpleNamespace(
        name=name,
        image=image,
        env=[SimpleNamespace(name=item) for item in env],
        command=list(command),
        args=list(args),
    )


class TestJvmHints:
    @pytest.mark.parametrize(
        ("image", "env", "command", "hint"),
        [
            ("ghcr.io/acme/orders:1.2", ["JAVA_TOOL_OPTIONS"], [], "JAVA_TOOL_OPTIONS"),
            ("ghcr.io/acme/orders:1.2", ["PORT", "JAVA_OPTS"], [], "JAVA_OPTS"),
            ("ghcr.io/acme/orders:1.2", [], ["java", "-Xmx512m", "-jar", "app.jar"], "-Xmx"),
            ("ghcr.io/acme/orders:1.2", [], ["/opt/java/openjdk/bin/java"], "java command"),
            ("eclipse-temurin:21-jre", [], [], "image eclipse-temurin"),
            ("docker.elastic.co/elasticsearch/elasticsearch:8.15.0", [], [], "image elasticsearch"),
            ("confluentinc/cp-kafka:7.6.0", [], [], "image cp-kafka"),
            ("registry:5000/acme/app:1.0-jre", [], [], "image app"),
        ],
    )
    def test_detected(self, image, env, command, hint):
        assert jvm_hint(image, env, command) == hint

    @pytest.mark.parametrize(
        "image",
        ["quay.io/prometheus/prometheus:v3.6.0", "nginx:1.27", "registry:5000/acme/javascript-app:1", "python:3.13"],
    )
    def test_not_detected(self, image):
        assert jvm_hint(image, ["PORT"], ["python", "-m", "app"]) is None


class TestTargets:
    def test_pods_resolve_to_known_targets(self):
        known = {STS, API, ("kube-system", "DaemonSet", "kube-proxy"), ("tools", "Pod", "debug")}
        assert target_for_pod("monitoring", "prometheus-k8s-1", known) == STS
        assert target_for_pod("shop", "api-7d9f8b6c5d-abcde", known) == API
        assert target_for_pod("kube-system", "kube-proxy-gsp47", known) == ("kube-system", "DaemonSet", "kube-proxy")
        assert target_for_pod("tools", "debug", known) == ("tools", "Pod", "debug")
        assert target_for_pod("shop", "unknown-0", known) is None
        assert target_for_pod("shop", "anything", known, "Deployment", "api") == API


@pytest.mark.asyncio
async def test_scan_signals_are_per_target_container_and_count_duplicates_once():
    snapshot = _snapshot(
        _summary(STS, "prometheus"),
        _summary(STS, "config-reloader"),
        _summary(STS, ""),
        _summary(API, "web", cpu_limit=1000),
    )
    query = FakeQuery(
        {
            "count(kube_pod_container_status_restarts_total)": [_series(42)],
            "kube_pod_container_status_last_terminated_reason": [
                _series(2, namespace="monitoring", pod="prometheus-k8s-0", container="prometheus"),
                _series(1, namespace="monitoring", pod="prometheus-k8s-1", container="prometheus"),
            ],
            "container_oom_events_total": [
                _series(1, namespace="monitoring", pod="prometheus-k8s-0", container="prometheus"),
            ],
            "container_cpu_cfs_throttled_periods_total": [
                _series(30, namespace="shop", pod="api-7d9f8b6c5d-abcde", container="web"),
                _series(10, namespace="shop", pod="api-7d9f8b6c5d-fghij", container="web"),
            ],
            "container_cpu_cfs_periods_total": [
                _series(200, namespace="shop", pod="api-7d9f8b6c5d-abcde", container="web"),
                _series(200, namespace="shop", pod="api-7d9f8b6c5d-fghij", container="web"),
            ],
        }
    )

    await attach_reliability_signals(snapshot, Config(), start=START, end=END, query=query, list_pods=_no_pods)

    sts = snapshot.summaries[STS]
    assert sts["prometheus"].oom_kill_count == 3  # both replicas; KSM wins over the smaller cAdvisor count
    assert sts["config-reloader"].oom_kill_count == 0  # kube-state-metrics is scraped: none is a known zero
    assert sts[""].oom_kill_count == 3
    web = snapshot.summaries[API]["web"]
    assert web.throttling_ratio == pytest.approx(0.1)
    assert sts["prometheus"].throttling_ratio is None  # no CFS series: no CPU limit, unknown
    # Every query merges duplicate series of one container with max, over the lookback.
    signal_queries = [q for q in query.queries if not q.startswith("count(")]
    assert all(q.startswith("max by (namespace, pod, container)") for q in signal_queries)
    assert all(f"[{14 * 86400}s" in q for q in signal_queries)
    # An OOM kill is a restart whose termination reason, at that restart's step, is OOMKilled:
    # never every restart of a lookback that saw one OOM kill.
    (ksm,) = [q for q in signal_queries if "last_terminated_reason" in q]
    assert "sum_over_time((" in ksm and f"[{14 * 86400}s:5m]" in ksm
    assert "increase(kube_pod_container_status_restarts_total[5m])" in ksm
    assert "max_over_time(" not in ksm


@pytest.mark.asyncio
async def test_restarts_in_the_window_are_read_per_container():
    snapshot = _snapshot(_summary(API, "web"), _summary(API, "proxy"), _summary(API, ""))
    query = FakeQuery(
        {
            "count(kube_pod_container_status_restarts_total)": [_series(3)],
            "kube_pod_container_status_last_terminated_reason": [],
            "round(increase(kube_pod_container_status_restarts_total{": [
                _series(2, namespace="shop", pod="api-7d9f8b6c5d-abcde", container="web"),
                _series(1, namespace="shop", pod="api-7d9f8b6c5d-fghij", container="web"),
            ],
        }
    )

    await attach_reliability_signals(snapshot, Config(), start=START, end=END, query=query, list_pods=_no_pods)

    containers = snapshot.summaries[API]
    assert containers["web"].restarts_in_window == 3
    assert containers["proxy"].restarts_in_window == 0  # kube-state-metrics is scraped: a known zero
    assert containers[""].restarts_in_window == 3
    (restarts,) = [q for q in query.queries if "round(increase(kube_pod_container_status_restarts_total{" in q]
    assert f"[{14 * 86400}s]" in restarts


@pytest.mark.asyncio
async def test_without_kube_state_metrics_oom_kills_stay_unknown():
    snapshot = _snapshot(_summary(STS, "prometheus"))
    await attach_reliability_signals(snapshot, Config(), start=START, end=END, query=FakeQuery({}), list_pods=_no_pods)
    assert snapshot.summaries[STS]["prometheus"].oom_kill_count is None
    assert snapshot.summaries[STS]["prometheus"].restarts_in_window is None
    assert snapshot.summaries[STS]["prometheus"].throttling_ratio is None


@pytest.mark.asyncio
async def test_failures_leave_signals_unknown():
    async def broken_query(promql, at):
        raise RuntimeError("prometheus down")

    async def broken_pods():
        raise RuntimeError("api down")

    snapshot = _snapshot(_summary(STS, "prometheus"))
    await attach_reliability_signals(
        snapshot, Config(), start=START, end=END, query=broken_query, list_pods=broken_pods
    )
    summary = snapshot.summaries[STS]["prometheus"]
    assert (summary.oom_kill_count, summary.throttling_ratio, summary.jvm_hint) == (None, None, None)


def test_jvm_hints_from_pod_specs_follow_the_owner():
    snapshot = _snapshot(_summary(API, "web"), _summary(API, "sidecar"), _summary(API, ""))
    pods = [
        _pod(
            "shop",
            "api-7d9f8b6c5d-abcde",
            [_container("web", "ghcr.io/acme/api:1", env=("JAVA_TOOL_OPTIONS",)), _container("sidecar", "envoy:1")],
            owner=("ReplicaSet", "api-7d9f8b6c5d"),
        ),
        _pod("other", "solo", [_container("app", "tomcat:10")]),
    ]
    hints = jvm_hints_from_pods(pods, snapshot)
    assert hints == {(API, "web"): "JAVA_TOOL_OPTIONS"}
    signals = ReliabilitySignals(jvm=hints)
    apply_signals(snapshot, signals)
    assert snapshot.summaries[API]["web"].jvm_hint == "JAVA_TOOL_OPTIONS"
    assert snapshot.summaries[API]["sidecar"].jvm_hint is None
    assert snapshot.summaries[API][""].jvm_hint == "JAVA_TOOL_OPTIONS"


async def _no_pods() -> List[Any]:
    return []


@pytest.mark.asyncio
async def test_oom_kills_found_by_the_scan_drive_an_uncapped_memory_increase():
    summary = _summary(
        STS,
        "prometheus",
        memory_request=400 * MIB,
        memory=UsageStats(
            sample_count=4000, avg=300 * MIB, p50=300 * MIB, p90=330 * MIB, p95=340 * MIB, p99=350 * MIB, max=900 * MIB
        ),
    )
    snapshot = _snapshot(summary)
    query = FakeQuery(
        {
            "count(kube_pod_container_status_restarts_total)": [_series(1)],
            "kube_pod_container_status_last_terminated_reason": [
                _series(2, namespace="monitoring", pod="prometheus-k8s-0", container="prometheus")
            ],
        }
    )
    await attach_reliability_signals(snapshot, Config(), start=START, end=END, query=query, list_pods=_no_pods)

    context = OptimizationContext(config=Config(), usage=snapshot)
    (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)
    assert rec.recommended_memory_request_bytes == 990 * MIB  # max 900Mi x 1.1, above the 2x cap: OOM evidence
    assert "OOM-killed" in rec.reason and "observed maximum" in rec.reason


def test_risk_profile_reads_the_scan_signals_and_jvm_lowers_memory_decrease_confidence():
    plain = _summary(API, "web")
    jvm = _summary(API, "web", jvm_hint="JAVA_TOOL_OPTIONS", oom_kill_count=0)
    results = {}
    for name, summary in (("plain", plain), ("jvm", jvm)):
        context = OptimizationContext(config=Config(), usage=_snapshot(summary), analysis_window_seconds=14 * 86400)
        (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)
        assert rec.recommended_memory_request_bytes is not None
        assert rec.recommended_memory_request_bytes < 512 * MIB
        (enriched,) = enrich_recommendations([rec], context, context.config)
        results[name] = enriched
    assert "jvm_heap_not_observable" in results["jvm"].risk_factors
    assert "jvm_heap_not_observable" not in results["plain"].risk_factors
    plain_confidence = results["plain"].confidence
    assert plain_confidence is not None
    assert results["jvm"].confidence == pytest.approx(plain_confidence * JVM_MEMORY_DECREASE_CONFIDENCE)
    assert "JVM runtime suspected" in results["jvm"].reason


def test_container_kinds_from_pod_specs_mark_native_sidecars_and_init_containers():
    """H1: a native sidecar lives in initContainers, so its action must address that list."""
    snapshot = _snapshot(_summary(API, "web"), _summary(API, "proxy"), _summary(API, ""))
    pod = _pod(
        "shop",
        "api-7d9f8b6c5d-abcde",
        [_container("web", "ghcr.io/acme/api:1")],
        owner=("ReplicaSet", "api-7d9f8b6c5d"),
    )
    pod.spec.init_containers = [
        SimpleNamespace(name="proxy", image="envoy:1", restart_policy="Always"),
        SimpleNamespace(name="migrate", image="migrate:1", restart_policy=None),
    ]
    kinds = container_kinds_from_pods([pod], snapshot)
    assert kinds == {(API, "web"): "container", (API, "proxy"): "sidecar", (API, "migrate"): "init"}
    apply_signals(snapshot, ReliabilitySignals(container_kinds=kinds))
    assert snapshot.summaries[API]["web"].container_kind == "container"
    assert snapshot.summaries[API]["proxy"].container_kind == "sidecar"
    assert snapshot.summaries[API][""].container_kind is None


@pytest.fixture(autouse=True)
def _wall_clock_at_test_time(monkeypatch):
    """Re-read ``END``/``START`` for each test: a wall-clock window taken at import ages during a long suite."""
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    monkeypatch.setitem(globals(), "END", end)
    monkeypatch.setitem(globals(), "START", end - timedelta(days=14))
