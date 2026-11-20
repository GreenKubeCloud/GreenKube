"""F-04 / REC-09: static (mirror) pods never receive a recommendation.

kubeadm, kind, minikube and k3s run the control plane as static pods. Their
mirror pods are owned by the ``Node`` (annotation ``kubernetes.io/config.mirror``),
so the rollup merged etcd, kube-apiserver, scheduler and controller-manager into
one fake workload ``Node/<node>`` that received rightsizing and HPA advice whose
manifests live on the node, not in Git. They are left out of every analyzer with
a visible diagnostic, and their stored recommendations are retired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import models as k8s

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers.rightsizing_memory import MemoryRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from tests.core.optimization.helpers import NativeRecommender

MIB = 1024 * 1024
START = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=8)
STATIC = (("etcd-cp1", "etcd", 100), ("kube-apiserver-cp1", "kube-apiserver", 250))


def _row(pod, container, owner_kind, owner_name, namespace, hour, cpu_request, cpu_usage) -> CombinedMetric:
    return CombinedMetric(
        restart_count=0,
        pod_name=pod,
        namespace=namespace,
        owner_kind=owner_kind,
        owner_name=owner_name,
        container_name=container,
        timestamp=START + timedelta(hours=hour),
        duration_seconds=3600,
        cpu_request=cpu_request,
        memory_request=512 * MIB,
        cpu_usage_millicores=cpu_usage,
        memory_usage_bytes=100 * MIB,
        total_cost=0.05,
        cost_cpu=0.03,
        cost_ram=0.02,
        cost_source="opencost",
        node="cp1",
    )


def _rows():
    rows = []
    for hour in range(8 * 24):
        for pod, container, request in STATIC:
            usage = 3 if hour % 2 else 70  # very variable across the merged "replicas"
            rows.append(_row(pod, container, "Node", "cp1", "kube-system", hour, request, usage))
        rows.append(_row("api-7d9f8b6c5d-abcde", "web", "Deployment", "api", "shop", hour, 1000, 50))
    return rows


def _config() -> Config:
    return Config(RECOMMEND_SYSTEM_NAMESPACES=True)


def test_a_kubeadm_like_scan_emits_nothing_for_static_pods():
    recs = NativeRecommender(_config()).generate_recommendations(_rows(), analysis_window_seconds=8 * 86400)

    assert recs, "the Deployment still gets its advice"
    assert [rec for rec in recs if rec.owner_kind == "Node"] == []
    assert [rec for rec in recs if rec.namespace == "kube-system"] == []
    assert not any(
        rec.type == RecommendationType.AUTOSCALING_CANDIDATE and rec.owner_kind != "Deployment" for rec in recs
    )


def test_the_snapshot_leaves_static_pods_out_and_counts_them():
    context = OptimizationContext(config=_config(), metrics=_rows(), analysis_window_seconds=8 * 86400)

    snapshot = context.usage_snapshot()

    assert ("kube-system", "Node", "cp1") not in snapshot.summaries
    assert ("shop", "Deployment", "api") in snapshot.summaries
    assert context.static_pod_count == 2
    assert context.static_pod_targets == {("kube-system", "Node", "cp1")}


def test_the_source_run_reports_the_excluded_static_pods():
    context = OptimizationContext(config=_config(), metrics=_rows(), analysis_window_seconds=8 * 86400)
    analyzer = MemoryRightsizingAnalyzer(context.config)
    analyzer.analyze(context)

    notes = {note["code"]: note for note in analyzer.scan_diagnostics()}

    assert notes["static_pods_excluded"]["count"] == 2
    assert "static" in notes["static_pods_excluded"]["message"]


@pytest.mark.asyncio
@patch("greenkube.collectors.pod_collector.get_core_v1_api")
async def test_a_mirror_pod_without_a_node_owner_is_attributed_to_its_node(mock_get_api):
    """Older kubelets set only the annotation: the collector stores the Node owner anyway."""
    from greenkube.collectors.pod_collector import PodCollector

    container = k8s.V1Container(name="etcd", resources=k8s.V1ResourceRequirements(requests={"cpu": "100m"}))
    pod = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(
            name="etcd-cp1", namespace="kube-system", annotations={"kubernetes.io/config.mirror": "abc"}
        ),
        spec=k8s.V1PodSpec(containers=[container], node_name="cp1"),
    )
    api = MagicMock()
    api.list_pod_for_all_namespaces = AsyncMock(return_value=k8s.V1PodList(items=[pod]))
    mock_get_api.return_value = api

    [metric] = await PodCollector().collect()

    assert (metric.owner_kind, metric.owner_name) == ("Node", "cp1")


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "static.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


def _cpu(kind: str, name: str, container: str) -> Recommendation:
    return Recommendation(
        pod_name=name,
        namespace="kube-system" if kind == "Node" else "shop",
        container_name=container,
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind=kind,
        owner_name=name,
        description="cpu rightsizing",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
    )


async def test_stored_static_pod_recommendations_are_retired_with_a_reason(repo):
    engine = OptimizationEngine(sources=[])
    # Stored by a version before the fix.
    await repo.upsert_recommendations([engine._record(_cpu("Node", "cp1", "etcd"))])
    await engine._persist([_cpu("Deployment", "api", "web")], repo)

    await engine._persist([_cpu("Deployment", "api", "web")], repo)

    assert {r.owner_kind for r in await repo.get_active_recommendations()} == {"Deployment"}
    [retired] = [r for r in await repo.get_recommendations_by_statuses(["stale"]) if r.owner_kind == "Node"]
    assert retired.id is not None
    events = await repo.get_events(retired.id)
    assert any(
        event.event_type == "stale" and getattr(event.payload, "reason", None) == "static_pod" for event in events
    )


async def test_a_static_pod_recommendation_from_any_source_is_not_stored(repo):
    engine = OptimizationEngine(sources=[])

    await engine._persist([_cpu("Node", "cp1", "etcd"), _cpu("Deployment", "api", "web")], repo)

    assert {r.owner_kind for r in await repo.get_active_recommendations()} == {"Deployment"}
