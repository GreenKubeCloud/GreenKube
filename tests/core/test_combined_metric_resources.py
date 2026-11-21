# tests/core/test_combined_metric_resources.py
"""
Tests for the extended CombinedMetric model with additional resource fields.

Validates that CombinedMetric correctly stores:
- Pod restart count
- GPU usage
"""

from datetime import datetime, timezone

from greenkube.models.metrics import CombinedMetric


class TestCombinedMetricResourceFields:
    """Tests for new resource fields on CombinedMetric."""

    def test_default_values_for_new_fields(self):
        """New fields should default to None or 0."""
        metric = CombinedMetric(
            pod_name="test-pod",
            namespace="default",
        )
        assert metric.gpu_usage_millicores is None
        assert metric.restart_count is None

    def test_set_restart_count(self):
        """Restart count should store values correctly."""
        metric = CombinedMetric(
            pod_name="crash-loop",
            namespace="test",
            restart_count=5,
        )
        assert metric.restart_count == 5

    def test_set_gpu_usage(self):
        """GPU usage field should store values correctly."""
        metric = CombinedMetric(
            pod_name="ml-training",
            namespace="ai",
            gpu_usage_millicores=750,
        )
        assert metric.gpu_usage_millicores == 750

    def test_full_combined_metric_with_all_resources(self):
        """All fields (old + new) should coexist correctly."""
        ts = datetime(2025, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
        metric = CombinedMetric(
            pod_name="full-pod",
            namespace="production",
            total_cost=1.5,
            co2e_grams=25.0,
            pue=1.2,
            grid_intensity=50.0,
            joules=500000,
            cpu_request=1000,
            memory_request=2_147_483_648,
            cpu_usage_millicores=750,
            memory_usage_bytes=1_073_741_824,
            gpu_usage_millicores=500,
            restart_count=0,
            timestamp=ts,
            node="node-1",
            owner_kind="Deployment",
            owner_name="full-app",
        )
        assert metric.pod_name == "full-pod"
        assert metric.cpu_request == 1000
        assert metric.gpu_usage_millicores == 500
        assert metric.restart_count == 0

    def test_model_dump_includes_new_fields(self):
        """model_dump() should include all new fields."""
        metric = CombinedMetric(
            pod_name="test",
            namespace="default",
            restart_count=1,
        )
        data = metric.model_dump()
        assert "gpu_usage_millicores" in data
        assert "restart_count" in data
