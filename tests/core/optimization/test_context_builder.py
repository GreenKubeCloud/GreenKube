# tests/core/optimization/test_context_builder.py
"""Tests for the OptimizationContext builder (single data-loading path)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.core.config import get_config
from greenkube.core.optimization.context_builder import ContextBuilder
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo


def _metric(pod_name="pod", namespace="default"):
    return CombinedMetric(
        pod_name=pod_name,
        namespace=namespace,
        total_cost=0.01,
        co2e_grams=1.0,
        joules=1000.0,
        timestamp=datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc),
    )


def _node():
    return NodeInfo(
        name="node-1",
        instance_type="m5.large",
        zone="eu-west-3a",
        region="eu-west-3",
        cloud_provider="aws",
        architecture="amd64",
        cpu_capacity_cores=2.0,
        memory_capacity_bytes=8 * 1024**3,
        timestamp=datetime(2026, 5, 26, 11, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def combined_repo():
    repo = AsyncMock()
    repo.read_combined_metrics_smart = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def node_repo():
    repo = AsyncMock()
    repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    return repo


@pytest.fixture(autouse=True)
def mock_side_collectors():
    """Mock HPA/PV/LB collectors so no real K8s call is attempted."""
    with (
        patch("greenkube.core.optimization.context_builder.HPACollector") as hpa_cls,
        patch("greenkube.core.optimization.context_builder.PVCollector") as pv_cls,
        patch("greenkube.core.optimization.context_builder.LoadBalancerCollector") as lb_cls,
    ):
        hpa = MagicMock()
        hpa.collect = AsyncMock(return_value=set())
        hpa_cls.return_value = hpa
        pv = MagicMock()
        pv.collect = AsyncMock(return_value=[])
        pv_cls.return_value = pv
        lb = MagicMock()
        lb.collect = AsyncMock(return_value=[])
        lb_cls.return_value = lb
        yield hpa_cls, pv_cls, lb_cls


@pytest.fixture(autouse=True)
def mock_active_namespaces():
    with patch(
        "greenkube.core.optimization.context_builder.get_active_k8s_namespaces",
        new=AsyncMock(return_value=None),
    ) as m:
        yield m


class TestBuild:
    async def test_empty_metrics_returns_empty_context_without_side_inputs(
        self, combined_repo, node_repo, mock_side_collectors
    ):
        builder = ContextBuilder(get_config())
        context = await builder.build(combined_repo, node_repo)

        assert context.metrics == []
        assert context.analysis_window_seconds is not None
        node_repo.get_latest_snapshots_before.assert_not_awaited()
        mock_side_collectors[0].return_value.collect.assert_not_awaited()

    async def test_filters_metrics_from_inactive_namespaces(self, combined_repo, node_repo, mock_active_namespaces):
        combined_repo.read_combined_metrics_smart = AsyncMock(
            return_value=[_metric(namespace="active"), _metric(namespace="dead")]
        )
        mock_active_namespaces.return_value = {"active"}

        builder = ContextBuilder(get_config())
        context = await builder.build(combined_repo, node_repo)

        assert [m.namespace for m in context.metrics] == ["active"]

    async def test_keeps_all_metrics_when_k8s_unavailable(self, combined_repo, node_repo, mock_active_namespaces):
        combined_repo.read_combined_metrics_smart = AsyncMock(
            return_value=[_metric(namespace="active"), _metric(namespace="dead")]
        )
        mock_active_namespaces.return_value = None

        builder = ContextBuilder(get_config())
        context = await builder.build(combined_repo, node_repo)

        assert len(context.metrics) == 2

    async def test_collects_side_inputs_and_window(self, combined_repo, node_repo):
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_metric()])
        node_repo.get_latest_snapshots_before = AsyncMock(return_value=[_node()])

        builder = ContextBuilder(get_config())
        context = await builder.build(combined_repo, node_repo)

        assert len(context.node_infos) == 1
        assert context.hpa_targets == set()
        assert context.persistent_volumes == []
        assert context.load_balancers == []
        expected_seconds = get_config().RECOMMENDATION_LOOKBACK_DAYS * 86_400
        assert context.analysis_window_seconds is not None
        assert abs(context.analysis_window_seconds - expected_seconds) < 5

    async def test_continues_when_node_repo_fails(self, combined_repo, node_repo):
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_metric()])
        node_repo.get_latest_snapshots_before = AsyncMock(side_effect=RuntimeError("node DB down"))

        builder = ContextBuilder(get_config())
        context = await builder.build(combined_repo, node_repo)

        assert context.node_infos == []

    async def test_continues_when_hpa_collector_fails(self, combined_repo, node_repo, mock_side_collectors):
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_metric()])
        hpa_cls, _, _ = mock_side_collectors
        hpa_cls.return_value.collect = AsyncMock(side_effect=RuntimeError("k8s unreachable"))

        builder = ContextBuilder(get_config())
        context = await builder.build(combined_repo, node_repo)

        assert context.hpa_targets is None


class TestBuildFromMetrics:
    async def test_filters_by_namespace(self, node_repo):
        builder = ContextBuilder(get_config())
        context = await builder.build_from_metrics(
            [_metric(namespace="a"), _metric(namespace="b")],
            node_repo,
            namespace="a",
        )
        assert [m.namespace for m in context.metrics] == ["a"]

    async def test_empty_metrics_returns_empty_context(self, node_repo):
        builder = ContextBuilder(get_config())
        context = await builder.build_from_metrics([], node_repo)

        assert context.metrics == []
        node_repo.get_latest_snapshots_before.assert_not_awaited()

    async def test_collects_side_inputs(self, node_repo):
        node_repo.get_latest_snapshots_before = AsyncMock(return_value=[_node()])
        builder = ContextBuilder(get_config())
        context = await builder.build_from_metrics([_metric()], node_repo, analysis_window_seconds=3600)

        assert context.analysis_window_seconds == 3600
        assert len(context.node_infos) == 1
