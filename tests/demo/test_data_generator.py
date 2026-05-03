# tests/demo/test_data_generator.py
"""
Unit tests for the demo data generator module.
Validates that generated data is realistic, consistent, and correctly shaped.
"""

from datetime import datetime, timedelta, timezone

from greenkube.demo.data_generator import (
    DEMO_NODES,
    DEMO_WORKLOADS,
    NODE_STORIES,
    generate_carbon_intensity_history,
    generate_combined_metrics,
    generate_node_snapshots,
    generate_recommendations,
)
from greenkube.models.metrics import CombinedMetric, RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.node import NodeInfo


class TestGenerateNodeSnapshots:
    """Tests for generate_node_snapshots."""

    def test_generates_scd2_change_points_only(self):
        """Node history should emit only chronological SCD2 change points."""
        nodes = generate_node_snapshots(days=30)

        expected_records = (
            (2 * len(NODE_STORIES))
            + sum(1 for story in NODE_STORIES if story.get("downsized_days_ago") is not None)
            + sum(1 for story in NODE_STORIES if story.get("retired_days_ago") is not None)
        )
        timestamps = [node.timestamp for node in nodes if node.timestamp is not None]

        assert len(nodes) == expected_records
        assert timestamps == sorted(timestamps)

    def test_returns_node_info_objects(self):
        """All returned items are NodeInfo instances."""
        nodes = generate_node_snapshots(days=1)
        for node in nodes:
            assert isinstance(node, NodeInfo)

    def test_current_topology_matches_story(self):
        """The current demo topology must match the GreenOptic story."""
        assert len(DEMO_NODES) == 12
        assert all(node["cloud_provider"] == "aws" for node in DEMO_NODES)
        assert {node["instance_type"] for node in DEMO_NODES} == {"m8g.2xlarge", "m8g.xlarge"}
        assert sum(1 for node in DEMO_NODES if node["instance_type"] == "m8g.2xlarge") == 6
        assert sum(1 for node in DEMO_NODES if node["region"].startswith("eu-")) == 4
        assert sum(1 for node in DEMO_NODES if node["region"].startswith("us-")) == 4
        assert sum(1 for node in DEMO_NODES if node["region"].startswith("ap-")) == 4
        capacities = {
            (node["instance_type"], node["cpu_capacity_cores"], node["memory_capacity_bytes"] // 1024**3)
            for node in DEMO_NODES
        }
        assert ("m8g.2xlarge", 8.0, 32) in capacities
        assert ("m8g.xlarge", 4.0, 16) in capacities

    def test_node_names_match_topology(self):
        """Generated snapshots must contain at least the current node names."""
        expected_names = {n["name"] for n in DEMO_NODES}
        nodes = generate_node_snapshots(days=1)
        actual_names = {n.name for n in nodes}
        assert expected_names.issubset(actual_names)

    def test_retired_nodes_have_inactive_terminal_snapshot(self):
        """Retired demo nodes should end with an inactive snapshot."""
        nodes = generate_node_snapshots(days=30)
        retired = {node.name for node in nodes if node.is_active is False}

        assert retired == {
            "eu-optic-buffer-05",
            "us-optic-buffer-05",
            "ap-optic-buffer-05",
        }

    def test_snapshots_cover_two_year_story(self):
        """Historical snapshots should span about two years and include retired nodes."""
        nodes = generate_node_snapshots(days=30)
        timestamps = [node.timestamp for node in nodes if node.timestamp is not None]
        assert timestamps
        assert (datetime.now(timezone.utc) - min(timestamps)).days >= 700
        assert len({node.name for node in nodes}) >= 15

    def test_timestamps_are_utc(self):
        """All timestamps have UTC timezone."""
        nodes = generate_node_snapshots(days=2)
        for node in nodes:
            assert node.timestamp is not None
            assert node.timestamp.tzinfo is not None

    def test_node_capacity_is_positive(self):
        """CPU and memory capacity must be positive."""
        nodes = generate_node_snapshots(days=1)
        for node in nodes:
            assert node.cpu_capacity_cores > 0
            assert node.memory_capacity_bytes > 0


class TestGenerateCarbonIntensityHistory:
    """Tests for generate_carbon_intensity_history."""

    def test_generates_hourly_records(self):
        """At least one record per hour for the specified dense days, plus sparse YTD records."""
        days = 3
        records = generate_carbon_intensity_history(days=days)
        assert len(records) >= days * 24

    def test_records_have_required_fields(self):
        """Each record has the ElectricityMaps-compatible fields."""
        records = generate_carbon_intensity_history(days=1)
        required_keys = {"zone", "carbonIntensity", "datetime", "isEstimated"}
        for record in records:
            assert required_keys.issubset(record.keys())

    def test_history_contains_story_zones(self):
        """The history should cover the initial and final AWS regions used in the demo story."""
        records = generate_carbon_intensity_history(days=7)
        zones = {record["zone"] for record in records}
        assert {
            "FR",
            "IE",
            "DE",
            "ES",
            "US-MIDA-PJM",
            "US-MIDW-MISO",
            "US-CAL-CISO",
            "US-NW-PACW",
            "IN-WE",
            "JP-TK",
            "SG",
            "JP-KY",
        }.issubset(zones)

    def test_history_covers_two_year_story(self):
        """Carbon history should span about two years for long-range storytelling."""
        records = generate_carbon_intensity_history(days=30)
        timestamps = [datetime.fromisoformat(record["datetime"].replace("Z", "+00:00")) for record in records]
        assert (max(timestamps) - min(timestamps)).days >= 700

    def test_intensity_is_positive(self):
        """Carbon intensity values must be positive."""
        records = generate_carbon_intensity_history(days=2)
        for record in records:
            assert record["carbonIntensity"] > 0

    def test_intensity_is_realistic(self):
        """Carbon intensity should stay within credible ranges across Europe, USA, and Asia."""
        records = generate_carbon_intensity_history(days=7)
        for record in records:
            assert 10 < record["carbonIntensity"] < 850


class TestGenerateCombinedMetrics:
    """Tests for generate_combined_metrics."""

    def test_generates_metrics_for_all_workloads(self):
        """Metrics are generated for every pod in every namespace, plus sparse YTD records."""
        metrics = generate_combined_metrics(days=1)
        total_pods = sum(len(pods) for pods in DEMO_WORKLOADS.values())
        # Dense: 1 day = 24 hours, one record per pod per hour; sparse YTD data is also included
        assert len(metrics) >= total_pods * 24

    def test_all_metrics_are_combined_metric(self):
        """All returned items are CombinedMetric instances."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert isinstance(m, CombinedMetric)

    def test_namespaces_match_demo_topology(self):
        """All namespaces in the output match the demo workload definition."""
        metrics = generate_combined_metrics(days=1)
        expected_ns = set(DEMO_WORKLOADS.keys())
        actual_ns = {m.namespace for m in metrics}
        assert actual_ns == expected_ns

    def test_workloads_cover_data_and_business_domains(self):
        """The demo workload must include both the data platform and GreenOptic business services."""
        assert {
            "argocd",
            "monitoring",
            "datahub",
            "dremio",
            "mageai",
            "minio",
            "superset",
            "commerce",
            "manufacturing",
            "website",
        }.issubset(set(DEMO_WORKLOADS))

    def test_co2_is_non_negative(self):
        """CO2 emissions should never be negative."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert m.co2e_grams >= 0

    def test_cost_is_non_negative(self):
        """Cost should never be negative."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert m.total_cost >= 0

    def test_energy_is_non_negative(self):
        """Energy (joules) should never be negative."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert m.joules >= 0

    def test_cpu_usage_within_bounds(self):
        """CPU usage should be positive for all records."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert m.cpu_usage_millicores >= 1

    def test_timestamps_span_requested_period(self):
        """Timestamps should span the requested number of days."""
        days = 3
        metrics = generate_combined_metrics(days=days)
        timestamps = [m.timestamp for m in metrics if m.timestamp]
        oldest = min(timestamps)
        newest = max(timestamps)
        span = (newest - oldest).total_seconds() / 3600
        # Should span at least (days-1)*24 hours
        assert span >= (days - 1) * 23

    def test_latest_metrics_stay_within_live_scrape_window(self):
        """Demo metrics should include a recent dense point for /prometheus/metrics."""
        metrics = generate_combined_metrics(days=1)

        latest = max(m.timestamp for m in metrics if m.timestamp is not None)
        assert datetime.now(timezone.utc) - latest <= timedelta(minutes=20)

    def test_metrics_cover_two_year_story(self):
        """Metrics history should keep roughly two years of sparse-to-dense records."""
        metrics = generate_combined_metrics(days=30)
        timestamps = [m.timestamp for m in metrics if m.timestamp]
        assert timestamps
        assert (max(timestamps) - min(timestamps)).days >= 700

    def test_calculation_version_set(self):
        """Metrics should have the demo calculation version."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert m.calculation_version is not None
            assert "demo" in m.calculation_version

    def test_metrics_use_mixed_story_instance_types(self):
        """Metrics should reflect the rightsized mix of current AWS Graviton nodes."""
        metrics = generate_combined_metrics(days=1)
        assert {m.node_instance_type for m in metrics if m.node_instance_type} == {"m8g.2xlarge", "m8g.xlarge"}

    def test_pue_is_realistic(self):
        """PUE should be between 1.0 and 2.0."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert 1.0 <= m.pue <= 2.0

    def test_grid_intensity_is_positive(self):
        """Grid intensity should be positive."""
        metrics = generate_combined_metrics(days=1)
        for m in metrics:
            assert m.grid_intensity > 0


class TestGenerateRecommendations:
    """Tests for generate_recommendations."""

    def test_generates_multiple_recommendations(self):
        """Should generate a meaningful number of recommendations."""
        recs = generate_recommendations()
        assert len(recs) >= len(RecommendationType) + 3

    def test_all_are_recommendation_records(self):
        """All returned items are RecommendationRecord instances."""
        recs = generate_recommendations()
        for rec in recs:
            assert isinstance(rec, RecommendationRecord)

    def test_diverse_recommendation_types(self):
        """Recommendations should cover multiple types."""
        recs = generate_recommendations()
        types = {rec.type for rec in recs}
        assert types == set(RecommendationType)

    def test_all_types_have_been_applied(self):
        """The demo should already show one applied recommendation of each type."""
        recs = generate_recommendations()
        applied_types = {rec.type for rec in recs if rec.status == RecommendationStatus.APPLIED}
        assert applied_types == set(RecommendationType)

    def test_has_zombie_pod_recommendation(self):
        """Should include a zombie pod recommendation."""
        recs = generate_recommendations()
        types = {rec.type for rec in recs}
        assert RecommendationType.ZOMBIE_POD in types

    def test_has_rightsizing_recommendation(self):
        """Should include a rightsizing recommendation."""
        recs = generate_recommendations()
        types = {rec.type for rec in recs}
        assert RecommendationType.RIGHTSIZING_CPU in types

    def test_has_carbon_aware_recommendation(self):
        """Should include a carbon-aware scheduling recommendation."""
        recs = generate_recommendations()
        types = {rec.type for rec in recs}
        assert RecommendationType.CARBON_AWARE_SCHEDULING in types

    def test_all_have_description(self):
        """Every recommendation must have a non-empty description."""
        recs = generate_recommendations()
        for rec in recs:
            assert rec.description
            assert len(rec.description) > 10

    def test_all_have_timestamps(self):
        """Every recommendation has a created_at timestamp."""
        recs = generate_recommendations()
        now = datetime.now(timezone.utc)
        for rec in recs:
            assert rec.created_at is not None
            assert rec.created_at <= now

    def test_recommendations_span_two_year_story(self):
        """Recommendations should show a historical adoption story over roughly two years."""
        recs = generate_recommendations()
        oldest = min(rec.created_at for rec in recs)
        newest = max(rec.created_at for rec in recs)
        assert newest <= datetime.now(timezone.utc)
        assert oldest <= datetime.now(timezone.utc) - timedelta(days=700)

    def test_applied_recommendations_have_realized_savings(self):
        """Applied recommendations must carry realized cost and CO2 savings."""
        applied = [rec for rec in generate_recommendations() if rec.status == RecommendationStatus.APPLIED]
        assert applied
        for rec in applied:
            assert rec.cost_saved is not None
            assert rec.cost_saved > 0
            assert rec.carbon_saved_co2e_grams is not None
            assert rec.carbon_saved_co2e_grams > 0

    def test_savings_are_positive(self):
        """Potential savings should be positive when set."""
        recs = generate_recommendations()
        for rec in recs:
            if rec.potential_savings_cost is not None:
                assert rec.potential_savings_cost > 0
            if rec.potential_savings_co2e_grams is not None:
                assert rec.potential_savings_co2e_grams > 0
            if rec.cost_saved is not None:
                assert rec.cost_saved > 0
            if rec.carbon_saved_co2e_grams is not None:
                assert rec.carbon_saved_co2e_grams > 0
