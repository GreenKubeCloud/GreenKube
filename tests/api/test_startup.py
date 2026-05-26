# tests/api/test_startup.py
"""Tests for src/greenkube/api/startup.py — startup recommendation scan."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.api.startup import run_startup_recommendation_scan
from greenkube.models.metrics import RecommendationRecord, RecommendationType


def _make_metric(pod_name="nginx", namespace="default"):
    from greenkube.models.metrics import CombinedMetric

    return CombinedMetric(
        pod_name=pod_name,
        namespace=namespace,
        total_cost=0.01,
        co2e_grams=2.5,
        pue=1.2,
        grid_intensity=50.0,
        joules=5000.0,
        cpu_request=250,
        memory_request=256 * 1024 * 1024,
        timestamp=datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc),
        duration_seconds=300,
        node="node-1",
        node_instance_type="m5.large",
        node_zone="eu-west-3a",
        emaps_zone="FR",
        is_estimated=False,
        estimation_reasons=[],
        embodied_co2e_grams=0.05,
    )


def _make_reco_record(pod_name="nginx", namespace="default"):
    return RecommendationRecord(
        pod_name=pod_name,
        namespace=namespace,
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU request",
        reason="Low utilisation",
        priority="medium",
        potential_savings_cost=0.50,
        potential_savings_co2e_grams=5.0,
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=100,
    )


@pytest.fixture
def mock_repos():
    """Patch all three factory functions to return mock repositories."""
    combined_repo = AsyncMock()
    node_repo = AsyncMock()
    reco_repo = AsyncMock()
    node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    reco_repo.upsert_recommendations = AsyncMock(return_value=0)
    reco_repo.reconcile_active_recommendations = AsyncMock(return_value=0)
    with (
        patch("greenkube.api.startup.get_combined_metrics_repository", return_value=combined_repo),
        patch("greenkube.api.startup.get_node_repository", return_value=node_repo),
        patch("greenkube.api.startup.get_recommendation_repository", return_value=reco_repo),
    ):
        yield combined_repo, node_repo, reco_repo


@pytest.fixture(autouse=True)
def mock_k8s_namespaces():
    """Bypass K8s API in all startup tests."""
    with patch(
        "greenkube.api.startup._get_active_k8s_namespaces",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_hpa():
    """Bypass HPA collector in all startup tests."""
    with patch("greenkube.api.startup.HPACollector") as cls:
        instance = AsyncMock()
        instance.collect = AsyncMock(return_value=set())
        cls.return_value = instance
        yield cls


@pytest.fixture(autouse=True)
def mock_update_metrics():
    """Suppress Prometheus gauge writes in all startup tests."""
    with patch("greenkube.api.startup.update_recommendation_metrics") as m:
        yield m


@pytest.mark.asyncio
class TestRunStartupRecommendationScan:
    """Unit tests for run_startup_recommendation_scan."""

    async def test_skips_when_no_metrics(self, mock_repos):
        """Should return early (and not call the recommender) when DB has no metrics."""
        combined_repo, _, reco_repo = mock_repos
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[])

        with patch("greenkube.api.startup.Recommender") as mock_recommender_cls:
            await run_startup_recommendation_scan()

        mock_recommender_cls.assert_not_called()
        reco_repo.upsert_recommendations.assert_not_called()
        reco_repo.reconcile_active_recommendations.assert_not_called()

    async def test_happy_path_upserts_and_reconciles(self, mock_repos, mock_update_metrics):
        """Should generate, persist, and emit metrics when metrics exist."""
        combined_repo, _, reco_repo = mock_repos
        metrics = [_make_metric()]
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=metrics)

        reco = _make_reco_record()
        mock_recommender = MagicMock()
        mock_recommender.generate_recommendations.return_value = [reco]

        with patch("greenkube.api.startup.Recommender", return_value=mock_recommender):
            await run_startup_recommendation_scan()

        mock_recommender.generate_recommendations.assert_called_once()
        reco_repo.upsert_recommendations.assert_awaited_once()
        reco_repo.reconcile_active_recommendations.assert_awaited_once_with(
            reco_repo.upsert_recommendations.call_args[0][0],
            namespace=None,
        )
        mock_update_metrics.assert_called_once()

    async def test_does_not_upsert_when_no_recommendations_generated(self, mock_repos):
        """Should skip upsert (but still reconcile) when recommender returns nothing."""
        combined_repo, _, reco_repo = mock_repos
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_make_metric()])

        mock_recommender = MagicMock()
        mock_recommender.generate_recommendations.return_value = []

        with patch("greenkube.api.startup.Recommender", return_value=mock_recommender):
            await run_startup_recommendation_scan()

        reco_repo.upsert_recommendations.assert_not_awaited()
        reco_repo.reconcile_active_recommendations.assert_awaited_once_with([], namespace=None)

    async def test_applies_namespace_filter_when_k8s_returns_namespaces(self, mock_repos):
        """Metrics from inactive namespaces should be filtered out before scanning."""
        combined_repo, _, reco_repo = mock_repos
        metrics = [_make_metric(namespace="active"), _make_metric(namespace="dead")]
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=metrics)

        mock_recommender = MagicMock()
        mock_recommender.generate_recommendations.return_value = []

        with (
            patch(
                "greenkube.api.startup._get_active_k8s_namespaces",
                new=AsyncMock(return_value={"active"}),
            ),
            patch("greenkube.api.startup.Recommender", return_value=mock_recommender),
        ):
            await run_startup_recommendation_scan()

        passed_metrics = mock_recommender.generate_recommendations.call_args[0][0]
        assert all(m.namespace == "active" for m in passed_metrics)
        assert len(passed_metrics) == 1

    async def test_skips_when_all_metrics_filtered_by_namespace(self, mock_repos):
        """Should skip the scan if all metrics are filtered out by namespace check."""
        combined_repo, _, reco_repo = mock_repos
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_make_metric(namespace="dead")])

        with (
            patch(
                "greenkube.api.startup._get_active_k8s_namespaces",
                new=AsyncMock(return_value={"live"}),
            ),
            patch("greenkube.api.startup.Recommender") as mock_recommender_cls,
        ):
            await run_startup_recommendation_scan()

        mock_recommender_cls.assert_not_called()
        reco_repo.reconcile_active_recommendations.assert_not_awaited()

    async def test_continues_when_node_repo_fails(self, mock_repos):
        """A node-repo failure should be logged and the scan should continue."""
        combined_repo, node_repo, reco_repo = mock_repos
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_make_metric()])
        node_repo.get_latest_snapshots_before = AsyncMock(side_effect=RuntimeError("node DB down"))

        mock_recommender = MagicMock()
        mock_recommender.generate_recommendations.return_value = []

        with patch("greenkube.api.startup.Recommender", return_value=mock_recommender):
            await run_startup_recommendation_scan()  # must not raise

        # Recommender is still called, but with empty node_infos
        call_kwargs = mock_recommender.generate_recommendations.call_args[1]
        assert call_kwargs["node_infos"] == []

    async def test_continues_when_hpa_collector_fails(self, mock_repos, mock_hpa):
        """An HPA-collector failure should be logged and the scan should continue."""
        combined_repo, _, _ = mock_repos
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_make_metric()])

        mock_hpa.return_value.collect = AsyncMock(side_effect=RuntimeError("k8s unreachable"))

        mock_recommender = MagicMock()
        mock_recommender.generate_recommendations.return_value = []

        with patch("greenkube.api.startup.Recommender", return_value=mock_recommender):
            await run_startup_recommendation_scan()  # must not raise

        call_kwargs = mock_recommender.generate_recommendations.call_args[1]
        assert call_kwargs["hpa_targets"] is None

    async def test_top_level_exception_is_swallowed(self, mock_repos):
        """Any unexpected top-level error must not propagate (API startup must succeed)."""
        combined_repo, _, _ = mock_repos
        combined_repo.read_combined_metrics_smart = AsyncMock(side_effect=Exception("catastrophic DB error"))

        await run_startup_recommendation_scan()  # must not raise

    async def test_passes_analysis_window_to_recommender(self, mock_repos):
        """analysis_window_seconds should match the configured lookback period."""
        combined_repo, _, _ = mock_repos
        combined_repo.read_combined_metrics_smart = AsyncMock(return_value=[_make_metric()])

        mock_recommender = MagicMock()
        mock_recommender.generate_recommendations.return_value = []

        with (
            patch("greenkube.api.startup.get_config") as mock_cfg,
            patch("greenkube.api.startup.Recommender", return_value=mock_recommender),
        ):
            cfg = MagicMock()
            cfg.RECOMMENDATION_LOOKBACK_DAYS = 7
            mock_cfg.return_value = cfg

            await run_startup_recommendation_scan()

        call_kwargs = mock_recommender.generate_recommendations.call_args[1]
        expected_seconds = 7 * 86_400
        assert abs(call_kwargs["analysis_window_seconds"] - expected_seconds) < 5
