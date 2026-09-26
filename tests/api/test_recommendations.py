# tests/api/test_recommendations.py
"""Tests for the recommendations endpoint."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import get_config
from greenkube.models.metrics import RecommendationRecord, RecommendationSavingsSummary, RecommendationType


class TestRecommendationsEndpoint:
    """Tests for GET /api/v1/recommendations."""

    def test_recommendations_returns_200(self, client):
        """Should return 200 even with no data."""
        response = client.get("/api/v1/recommendations")
        assert response.status_code == 200

    def test_recommendations_returns_empty_list(self, client):
        """Should return an empty list when no metrics exist."""
        response = client.get("/api/v1/recommendations")
        data = response.json()
        assert data == []

    def test_recommendations_with_zombie_pod(self, client, mock_combined_metrics_repo):
        """Should detect a zombie pod (cost > threshold, energy < threshold)."""
        from datetime import datetime, timezone

        from greenkube.models.metrics import CombinedMetric

        zombie_metric = CombinedMetric(
            pod_name="zombie-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.0,
            joules=100.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[zombie_metric])
        response = client.get("/api/v1/recommendations")
        data = response.json()
        assert len(data) >= 1
        assert any(r["type"] == "ZOMBIE_POD" for r in data)

    def test_recommendation_potential_savings_are_annualized(self, client, mock_combined_metrics_repo):
        """Potential savings returned by the API should be annual projections from the lookback window."""
        from greenkube.models.metrics import CombinedMetric

        zombie_metric = CombinedMetric(
            pod_name="zombie-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.02,
            joules=100.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics_smart = AsyncMock(return_value=[zombie_metric])

        response = client.get("/api/v1/recommendations")

        assert response.status_code == 200
        zombie_rec = next(r for r in response.json() if r["type"] == "ZOMBIE_POD")
        annualization_factor = 365 / get_config().RECOMMENDATION_LOOKBACK_DAYS
        assert zombie_rec["potential_savings_cost"] == pytest.approx(0.05 * annualization_factor)
        assert zombie_rec["potential_savings_co2e_grams"] == pytest.approx(0.02 * annualization_factor)

    def test_recommendations_filter_by_namespace(self, client, mock_combined_metrics_repo):
        """Should filter recommendations by namespace."""
        from datetime import datetime, timezone

        from greenkube.models.metrics import CombinedMetric

        metrics = [
            CombinedMetric(
                pod_name="zombie-1",
                namespace="team-a",
                total_cost=0.05,
                co2e_grams=0.0,
                joules=100.0,
                cpu_request=100,
                memory_request=128 * 1024 * 1024,
                timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
                duration_seconds=300,
            ),
            CombinedMetric(
                pod_name="zombie-2",
                namespace="team-b",
                total_cost=0.05,
                co2e_grams=0.0,
                joules=100.0,
                cpu_request=100,
                memory_request=128 * 1024 * 1024,
                timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
                duration_seconds=300,
            ),
        ]
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/recommendations?namespace=team-a")
        data = response.json()
        assert all(r["namespace"] == "team-a" for r in data)

    def test_recommendations_contain_expected_fields(self, client, mock_combined_metrics_repo):
        """Each recommendation should contain expected fields."""
        from greenkube.models.metrics import CombinedMetric

        zombie = CombinedMetric(
            pod_name="zombie-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.0,
            joules=100.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[zombie])
        response = client.get("/api/v1/recommendations")
        data = response.json()
        if data:
            rec = data[0]
            for field in ["pod_name", "namespace", "type", "description"]:
                assert field in rec, f"Missing field: {field}"

    def test_recommendations_include_orphaned_persistent_volumes(
        self, client, mock_combined_metrics_repo, mock_pv_collector
    ):
        """Orphaned PVs collected from the cluster should surface as recommendations."""
        from greenkube.collectors.pv_collector import OrphanedPV
        from greenkube.models.metrics import CombinedMetric

        metric = CombinedMetric(
            pod_name="app-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.02,
            joules=5000.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[metric])
        mock_pv_collector.return_value.collect.return_value = [
            OrphanedPV(
                name="pvc-dead",
                phase="Released",
                capacity_bytes=100 * 1024**3,
                claim_namespace="default",
                claim_name="gone-claim",
                reclaim_policy="Retain",
            )
        ]

        response = client.get("/api/v1/recommendations")

        assert response.status_code == 200
        pv_recs = [r for r in response.json() if r["type"] == "ORPHANED_PERSISTENT_VOLUME"]
        assert len(pv_recs) == 1
        assert pv_recs[0]["pod_name"] == "pvc-dead"
        assert pv_recs[0]["scope"] == "cluster"
        # 100 GiB at the default $0.10/GiB-month → $120/year (OpenCost enrichment is mocked off)
        assert pv_recs[0]["potential_savings_cost"] == pytest.approx(120.0)
        assert pv_recs[0]["potential_savings_co2e_grams"] is None

    def test_recommendations_include_orphaned_load_balancers(
        self, client, mock_combined_metrics_repo, mock_lb_collector
    ):
        """Orphaned LoadBalancer Services collected from the cluster should surface as recommendations."""
        from greenkube.collectors.lb_collector import OrphanedLoadBalancer
        from greenkube.models.metrics import CombinedMetric

        metric = CombinedMetric(
            pod_name="app-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.02,
            joules=5000.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[metric])
        mock_lb_collector.return_value.collect.return_value = [
            OrphanedLoadBalancer(
                name="dead-lb",
                namespace="legacy",
                endpoint_count=0,
                external_ip="1.2.3.4",
            )
        ]

        response = client.get("/api/v1/recommendations")

        assert response.status_code == 200
        lb_recs = [r for r in response.json() if r["type"] == "ORPHANED_LOAD_BALANCER"]
        assert len(lb_recs) == 1
        assert lb_recs[0]["pod_name"] == "dead-lb"
        assert lb_recs[0]["namespace"] == "legacy"
        assert lb_recs[0]["scope"] == "cluster"
        # Flat estimate at the default $18.00/month → $216/year (OpenCost enrichment is mocked off)
        assert lb_recs[0]["potential_savings_cost"] == pytest.approx(216.0)
        assert lb_recs[0]["potential_savings_co2e_grams"] is None

    def test_savings_summary_passes_last_time_window_to_repository(self, client, mock_reco_repo):
        """Savings should be filtered by the selected dashboard time window."""
        mock_reco_repo.get_savings_summary = AsyncMock(
            return_value=RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=42,
                total_cost_saved=1.5,
                applied_count=2,
            )
        )

        response = client.get("/api/v1/recommendations/savings?namespace=prod&last=7d")

        assert response.status_code == 200
        assert mock_reco_repo.get_savings_summary.await_args is not None
        kwargs = mock_reco_repo.get_savings_summary.await_args.kwargs
        assert kwargs["namespace"] == "prod"
        assert kwargs["start"] < kwargs["end"]
        assert kwargs["start"].tzinfo is not None
        assert kwargs["end"].tzinfo is not None

    def test_savings_summary_supports_ytd_window(self, client, mock_reco_repo):
        """YTD should resolve to January 1st of the current UTC year."""
        mock_reco_repo.get_savings_summary = AsyncMock(return_value=RecommendationSavingsSummary())

        response = client.get("/api/v1/recommendations/savings?last=ytd")

        assert response.status_code == 200
        assert mock_reco_repo.get_savings_summary.await_args is not None
        start = mock_reco_repo.get_savings_summary.await_args.kwargs["start"]
        assert start.month == 1
        assert start.day == 1
        assert start.hour == 0
        assert start.tzinfo is not None

    def test_savings_summary_uses_ledger_for_selected_window(self, client, mock_reco_repo, mock_savings_repo):
        """Savings windows should count ongoing ledger rows, not only recommendations applied in the window."""
        mock_savings_repo.get_window_totals = AsyncMock(
            return_value={"RIGHTSIZING_CPU": {"co2e_saved_grams": 42.0, "cost_saved_dollars": 1.5}}
        )
        mock_reco_repo.get_savings_summary = AsyncMock(
            return_value=RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=0.0,
                total_cost_saved=0.0,
                applied_count=1,
            )
        )

        response = client.get("/api/v1/recommendations/savings?namespace=prod&last=7d")

        assert response.status_code == 200
        data = response.json()
        assert data["total_carbon_saved_co2e_grams"] == 42.0
        assert data["total_cost_saved"] == 1.5
        assert data["applied_count"] == 1
        mock_savings_repo.get_window_totals.assert_awaited_once()

    def test_savings_summary_without_window_uses_repository_summary(self, client, mock_reco_repo, mock_savings_repo):
        """Unbounded summaries should keep repository fallback semantics and namespace filtering."""
        mock_savings_repo.get_cumulative_totals = AsyncMock(
            return_value={"RIGHTSIZING_CPU": {"co2e_saved_grams": 999.0, "cost_saved_dollars": 99.0}}
        )
        mock_reco_repo.get_savings_summary = AsyncMock(
            return_value=RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=12.0,
                total_cost_saved=1.2,
                applied_count=2,
            )
        )

        response = client.get("/api/v1/recommendations/savings?namespace=prod")

        assert response.status_code == 200
        data = response.json()
        assert data["total_carbon_saved_co2e_grams"] == 12.0
        assert data["total_cost_saved"] == 1.2
        assert data["applied_count"] == 2
        mock_savings_repo.get_cumulative_totals.assert_not_awaited()
        mock_savings_repo.get_window_totals.assert_not_awaited()

    def test_top_recommendations_defaults_to_co2_savings(self, client, mock_reco_repo):
        """The actionable endpoint should return ranked recommendation DTOs by projected CO2 savings."""
        mock_reco_repo.get_top_recommendations = AsyncMock(
            return_value=[
                RecommendationRecord(
                    id=42,
                    pod_name="checkout-api",
                    namespace="production",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="Reduce checkout-api CPU request",
                    priority="high",
                    scope="workload",
                    potential_savings_co2e_grams=6200.0,
                    potential_savings_cost=148.5,
                )
            ]
        )

        response = client.get("/api/v1/recommendations/top")

        assert response.status_code == 200
        data = response.json()
        assert data == [
            {
                "rank": 1,
                "id": 42,
                "type": "RIGHTSIZING_CPU",
                "namespace": "production",
                "resource": "checkout-api",
                "scope": "workload",
                "priority": "high",
                "description": "Reduce checkout-api CPU request",
                "reason": "",
                "sort_metric": "co2",
                "sort_value": 6200.0,
                "projected_savings_co2e_grams": 6200.0,
                "projected_savings_cost": 148.5,
                "source": "greenkube",
                "risk_level": None,
                "confidence": None,
                "effort": None,
                "ranking_score": None,
                "ranking_factors": {},
                "expires_at": None,
            }
        ]
        mock_reco_repo.get_top_recommendations.assert_awaited_once_with(
            limit=5,
            savings_metric="co2",
            namespace=None,
        )

    def test_top_recommendations_supports_cost_limit_and_namespace(self, client, mock_reco_repo):
        """The actionable endpoint should expose the requested ranking controls to repository storage."""
        mock_reco_repo.get_top_recommendations = AsyncMock(return_value=[])

        response = client.get("/api/v1/recommendations/top?metric=cost&limit=3&namespace=staging")

        assert response.status_code == 200
        assert response.json() == []
        mock_reco_repo.get_top_recommendations.assert_awaited_once_with(
            limit=3,
            savings_metric="cost",
            namespace="staging",
        )

    def test_top_recommendations_with_profile_ranks_active_records(self, client, mock_reco_repo):
        """A ranking profile should use the multi-criteria score over all active records."""
        mock_reco_repo.get_active_recommendations = AsyncMock(
            return_value=[
                RecommendationRecord(
                    id=1,
                    pod_name="low-risk",
                    namespace="production",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="safe",
                    potential_savings_co2e_grams=1000.0,
                    potential_savings_cost=10.0,
                    risk_level="low",
                    confidence=0.9,
                ),
                RecommendationRecord(
                    id=2,
                    pod_name="high-risk",
                    namespace="production",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="risky",
                    potential_savings_co2e_grams=1000.0,
                    potential_savings_cost=10.0,
                    risk_level="high",
                    confidence=0.9,
                ),
            ]
        )

        response = client.get("/api/v1/recommendations/top?profile=low_risk")

        assert response.status_code == 200
        data = response.json()
        assert data[0]["resource"] == "low-risk"
        assert data[0]["ranking_score"] is not None
        assert data[0]["risk_level"] == "low"
        mock_reco_repo.get_top_recommendations.assert_not_awaited()


class TestRecommendationDetail:
    """Tests for GET /api/v1/recommendations/{id}."""

    def test_detail_returns_full_record(self, client, mock_reco_repo):
        record = RecommendationRecord(
            id=7,
            pod_name="api",
            namespace="prod",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Reduce CPU",
            evidence={"observation_window_seconds": 604800, "sample_count": 100, "savings_method": "x"},
        )
        mock_reco_repo.get_recommendation_by_id = AsyncMock(return_value=record)

        response = client.get("/api/v1/recommendations/7")

        assert response.status_code == 200
        assert response.json()["id"] == 7

    def test_detail_returns_404_when_missing(self, client, mock_reco_repo):
        mock_reco_repo.get_recommendation_by_id = AsyncMock(return_value=None)

        response = client.get("/api/v1/recommendations/999")

        assert response.status_code == 404


class TestActiveRecommendationFilters:
    """Tests for source/risk/capability filters on the active list."""

    def test_filters_by_source(self, client, mock_reco_repo):
        from greenkube.models.metrics import RecommendationSource

        mock_reco_repo.get_active_recommendations = AsyncMock(
            return_value=[
                RecommendationRecord(
                    id=1,
                    pod_name="api",
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="vpa",
                    source=RecommendationSource.VPA,
                ),
                RecommendationRecord(
                    id=2,
                    pod_name="api",
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_MEMORY,
                    description="native",
                    source=RecommendationSource.GREENKUBE,
                ),
            ]
        )

        response = client.get("/api/v1/recommendations/active?source=vpa")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["source"] == "vpa"

    def test_filters_by_risk_level(self, client, mock_reco_repo):
        mock_reco_repo.get_active_recommendations = AsyncMock(
            return_value=[
                RecommendationRecord(
                    id=1,
                    pod_name="a",
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="risky",
                    risk_level="high",
                ),
                RecommendationRecord(
                    id=2,
                    pod_name="b",
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="safe",
                    risk_level="low",
                ),
            ]
        )

        response = client.get("/api/v1/recommendations/active?risk_level=high")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == 1
