# tests/api/test_recommendations.py
"""Tests for the recommendations endpoint."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.models.savings import SavingsAccrualTotal


async def _scan(combined_repo, node_repo, reco_repo, namespace=None) -> list[dict]:
    """Run one controller-style scan (the API itself never scans, E1) and serialize its output."""
    from greenkube.core.db import get_db_manager

    try:
        recommendations = await OptimizationEngine().refresh(combined_repo, node_repo, reco_repo, namespace=namespace)
    finally:
        # The scan opens the process-wide database manager (aiosqlite runs a worker thread per
        # connection): close it so no thread outlives the test session.
        await get_db_manager().close()
    return [RecommendationRecord.from_recommendation(rec).model_dump(mode="json") for rec in recommendations]


def _hot_rows(pod: str, namespace: str) -> list:
    """Samples of a pod using its whole CPU request: a request increase (no coverage minimum)."""
    from greenkube.models.metrics import CombinedMetric

    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=4)
    return [
        CombinedMetric(
            pod_name=pod,
            container_name="app",
            namespace=namespace,
            total_cost=0.05,
            co2e_grams=0.01,
            joules=1000.0,
            cpu_usage_millicores=100,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            memory_usage_bytes=32 * 1024 * 1024,
            timestamp=start + timedelta(minutes=5 * step),
            duration_seconds=300,
        )
        for step in range(48)
    ]


class TestRecommendationsEndpoint:
    """Tests for GET /api/v1/recommendations and the scans that populate it."""

    def test_recommendations_returns_200(self, client):
        """Should return 200 even with no data."""
        response = client.get("/api/v1/recommendations")
        assert response.status_code == 200

    def test_recommendations_returns_empty_list(self, client):
        """Should return an empty list when no metrics exist."""
        response = client.get("/api/v1/recommendations")
        data = response.json()
        assert data == []

    async def test_recommendations_filter_by_namespace(
        self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo
    ):
        """Should filter recommendations by namespace."""
        metrics = [*_hot_rows("hot-1", "team-a"), *_hot_rows("hot-2", "team-b")]
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        data = await _scan(mock_combined_metrics_repo, mock_node_repo, mock_reco_repo, namespace="team-a")
        assert data
        assert all(r["namespace"] == "team-a" for r in data)

    async def test_recommendations_contain_expected_fields(
        self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo
    ):
        """Each recommendation should contain expected fields."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=_hot_rows("hot", "default"))
        data = await _scan(mock_combined_metrics_repo, mock_node_repo, mock_reco_repo)
        assert data
        for field in ["pod_name", "namespace", "type", "description"]:
            assert data[0][field], f"Missing field: {field}"

    async def test_recommendations_include_orphaned_persistent_volumes(
        self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo, mock_pv_collector
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
                evidence=["phase Released: its claim default/gone-claim was deleted"],
                first_seen_at=datetime.now(timezone.utc) - timedelta(days=8),
            )
        ]

        data = await _scan(mock_combined_metrics_repo, mock_node_repo, mock_reco_repo)

        pv_recs = [r for r in data if r["type"] == "ORPHANED_PERSISTENT_VOLUME"]
        assert len(pv_recs) == 1
        assert pv_recs[0]["pod_name"] == "pvc-dead"
        assert pv_recs[0]["scope"] == "cluster"
        # 100 GiB at the default $0.10/GiB-month → $120/year (OpenCost enrichment is mocked off)
        assert pv_recs[0]["potential_savings_cost"] == pytest.approx(120.0)
        assert pv_recs[0]["potential_savings_co2e_grams"] is None

    async def test_recommendations_include_orphaned_load_balancers(
        self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo, mock_lb_collector
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
                evidence=["selector app=dead matches no pod in namespace legacy"],
                first_seen_at=datetime.now(timezone.utc) - timedelta(days=8),
            )
        ]

        data = await _scan(mock_combined_metrics_repo, mock_node_repo, mock_reco_repo)

        lb_recs = [r for r in data if r["type"] == "ORPHANED_LOAD_BALANCER"]
        assert len(lb_recs) == 1
        assert lb_recs[0]["pod_name"] == "dead-lb"
        assert lb_recs[0]["namespace"] == "legacy"
        assert lb_recs[0]["scope"] == "cluster"
        # Flat estimate at the default $18.00/month → $216/year (OpenCost enrichment is mocked off)
        assert lb_recs[0]["potential_savings_cost"] == pytest.approx(216.0)
        assert lb_recs[0]["potential_savings_co2e_grams"] is None

    def test_savings_report_passes_window_and_namespace_to_ledger(self, client, mock_reco_repo, mock_savings_repo):
        """The ledger is read for the selected dashboard window, including its partial first hour."""
        response = client.get("/api/v1/recommendations/savings?namespace=prod&last=7d")

        assert response.status_code == 200
        mock_reco_repo.get_applied_recommendations.assert_awaited_once_with(namespace="prod")
        args = mock_savings_repo.get_accrual_breakdown.await_args
        assert args is not None
        cluster, start, end = args.args
        assert cluster == "default"
        assert args.kwargs["namespace"] == "prod"
        assert start < end and start.tzinfo is not None and end.tzinfo is not None
        data = response.json()
        assert data["realized_to_date"]["start"] is not None
        assert data["realized_to_date"]["method"] == "measured"

    def test_savings_report_supports_ytd_window(self, client, mock_savings_repo):
        """YTD should resolve to January 1st of the current UTC year."""
        response = client.get("/api/v1/recommendations/savings?last=ytd")

        assert response.status_code == 200
        start = mock_savings_repo.get_accrual_breakdown.await_args.args[1]
        assert (start.month, start.day, start.hour) == (1, 1, 0)
        assert start.tzinfo is not None

    def test_savings_report_without_window_reads_all_accruals(self, client, mock_savings_repo):
        response = client.get("/api/v1/recommendations/savings")

        assert response.status_code == 200
        assert mock_savings_repo.get_accrual_breakdown.await_args.args[1:] == (None, None)
        assert response.json()["window_start"] is None

    def test_savings_report_never_mixes_projected_and_measured(self, client, mock_reco_repo, mock_savings_repo):
        """C0.14 / BUG-40: three separate blocks; projected accruals never count as realized."""
        mock_reco_repo.get_applied_recommendations = AsyncMock(
            return_value=[
                RecommendationRecord(
                    id=1,
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="pending",
                    status="applied",
                    verification_status="pending",
                    carbon_saved_co2e_grams=1000.0,
                    cost_saved=100.0,
                ),
                RecommendationRecord(
                    id=2,
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="verified",
                    status="verified",
                    verification_status="passed",
                    carbon_saved_co2e_grams=900.0,
                    cost_saved=90.0,
                    measured_co2e_saved_grams=500.0,
                    measured_cost_saved=50.0,
                ),
                RecommendationRecord(
                    id=3,
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="not realized",
                    status="applied",
                    verification_status="failed",
                    carbon_saved_co2e_grams=700.0,
                    cost_saved=70.0,
                ),
                RecommendationRecord(
                    id=4,
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="rollback",
                    status="rollback_review",
                    carbon_saved_co2e_grams=300.0,
                    cost_saved=30.0,
                ),
            ]
        )
        mock_savings_repo.get_accrual_breakdown = AsyncMock(
            return_value=[
                SavingsAccrualTotal(
                    namespace="prod", recommendation_type="RIGHTSIZING_CPU", method="projected", co2e_saved_grams=42.0
                ),
                SavingsAccrualTotal(
                    namespace="prod",
                    recommendation_type="RIGHTSIZING_CPU",
                    method="measured",
                    co2e_saved_grams=8.0,
                    cost_saved_dollars=0.8,
                ),
                SavingsAccrualTotal(
                    namespace="prod",
                    recommendation_type="RIGHTSIZING_CPU",
                    method="user_declared",
                    co2e_saved_grams=-2.0,
                ),
            ]
        )

        data = client.get("/api/v1/recommendations/savings?namespace=prod&last=7d").json()

        projected = data["projected_annual"]
        assert (projected["method"], projected["basis"]) == ("projected", "annual_rate")
        assert (projected["co2e_grams"], projected["cost"], projected["recommendation_count"]) == (1000.0, 100.0, 1)
        assert (data["run_rate_annual"]["co2e_grams"], data["run_rate_annual"]["recommendation_count"]) == (500.0, 1)
        assert data["realized_to_date"]["co2e_grams"] == 8.0
        assert data["projected_to_date"]["co2e_grams"] == 42.0
        assert data["user_declared_to_date"]["co2e_grams"] == -2.0
        assert (data["applied_count"], data["verified_count"]) == (4, 1)
        # Deprecated aliases keep the 0.3 UI working without mixing methods.
        assert data["total_carbon_saved_co2e_grams"] == 8.0
        assert data["measured_carbon_saved_co2e_grams"] == 8.0
        assert data["prorated_carbon_saved_co2e_grams"] == 42.0
        assert data["total_cost_saved"] == 0.8
        # No ledger v2 identity in this test: the v1 fallback is flagged, never silent (LED-04).
        assert [item.split(":")[0] for item in data["diagnostics"]] == ["ledger_v1_fallback"]
        assert data["realized_to_date"]["source"] == "ledger_v1"

    def test_savings_report_flags_legacy_compressed_rows(self, client, mock_savings_repo):
        mock_savings_repo.has_legacy_compressed_rows = AsyncMock(return_value=True)

        data = client.get("/api/v1/recommendations/savings?last=30d").json()

        assert any(item.startswith("legacy_compressed") for item in data["diagnostics"])

    def test_savings_report_returns_503_when_ledger_is_unreadable(self, client, mock_savings_repo):
        """BUG-42: no silent fallback to recommendation records."""
        mock_savings_repo.get_accrual_breakdown = AsyncMock(side_effect=RuntimeError("database is locked"))

        response = client.get("/api/v1/recommendations/savings?last=7d")

        assert response.status_code == 503
        assert response.json()["detail"] == "Savings ledger unavailable."

    def test_savings_report_returns_503_when_records_are_unreadable(self, client, mock_reco_repo):
        mock_reco_repo.get_applied_recommendations = AsyncMock(side_effect=RuntimeError("down"))

        assert client.get("/api/v1/recommendations/savings").status_code == 503

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
        assert len(data) == 1
        row = data[0]
        value = row.pop("value")
        eligibility = row.pop("eligibility")
        score = row.pop("ranking_score")
        factors = row.pop("ranking_factors")
        assert row == {
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
            "expires_at": None,
        }
        # The live decision: expected value in currency and delivery eligibility (I1, F10).
        assert score == value["ev"]
        assert sum(factors.values()) == pytest.approx(score, abs=0.05)
        assert eligibility["can_open_pr"] is False
        assert {blocker["code"] for blocker in eligibility["blockers"]} >= {"action_unavailable"}
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

    def test_source_filter_matches_every_source_behind_the_consensus(self, client, mock_reco_repo):
        """E6/G1: ``?source=goldilocks`` finds a VPA-chosen recommendation Goldilocks also supports."""
        from greenkube.models.metrics import RecommendationSource

        mock_reco_repo.get_active_recommendations = AsyncMock(
            return_value=[
                RecommendationRecord(
                    id=1,
                    pod_name="api",
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="vpa chosen, goldilocks and krr agree",
                    source=RecommendationSource.VPA,
                    consensus={"policy": "safest", "source_ids": ["vpa", "goldilocks", "krr"]},
                ),
                RecommendationRecord(
                    id=2,
                    pod_name="db",
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_MEMORY,
                    description="native only",
                    source=RecommendationSource.GREENKUBE,
                    consensus={"policy": "safest", "source_ids": ["greenkube.rightsizing_memory"]},
                ),
            ]
        )

        goldilocks = client.get("/api/v1/recommendations/active?source=goldilocks").json()
        krr = client.get("/api/v1/recommendations/active?source=krr").json()
        vpa = client.get("/api/v1/recommendations/active?source=vpa").json()

        assert [r["id"] for r in goldilocks] == [1]
        assert [r["id"] for r in krr] == [1]
        assert [r["id"] for r in vpa] == [1]

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
