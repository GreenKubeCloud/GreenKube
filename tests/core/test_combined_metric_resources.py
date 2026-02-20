# tests/core/test_combined_metric_resources.py
"""
Tests for the extended CombinedMetric model with additional resource fields.

Validates that CombinedMetric correctly stores:
- Network I/O (bytes received/transmitted)
- Disk I/O (bytes read/written)
- Storage request/usage
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
        assert metric.network_receive_bytes is None
        assert metric.network_transmit_bytes is None
        assert metric.disk_read_bytes is None
        assert metric.disk_write_bytes is None
        assert metric.storage_request_bytes is None
        assert metric.storage_usage_bytes is None
        assert metric.gpu_usage_millicores is None
        assert metric.restart_count is None
        assert metric.ephemeral_storage_request_bytes is None
        assert metric.ephemeral_storage_usage_bytes is None

    def test_set_network_io_fields(self):
        """Network I/O fields should store values correctly."""
        metric = CombinedMetric(
            pod_name="web-app",
            namespace="production",
            network_receive_bytes=1048576,
            network_transmit_bytes=524288,
        )
        assert metric.network_receive_bytes == 1048576
        assert metric.network_transmit_bytes == 524288

    def test_set_disk_io_fields(self):
        """Disk I/O fields should store values correctly."""
        metric = CombinedMetric(
            pod_name="db-pod",
            namespace="production",
            disk_read_bytes=2097152,
            disk_write_bytes=4194304,
        )
        assert metric.disk_read_bytes == 2097152
        assert metric.disk_write_bytes == 4194304

    def test_set_storage_fields(self):
        """Storage request/usage fields should store values correctly."""
        metric = CombinedMetric(
            pod_name="stateful-app",
            namespace="data",
            storage_request_bytes=10_737_418_240,  # 10 GiB
            storage_usage_bytes=5_368_709_120,  # 5 GiB
            ephemeral_storage_request_bytes=1_073_741_824,  # 1 GiB
            ephemeral_storage_usage_bytes=536_870_912,  # 512 MiB
        )
        assert metric.storage_request_bytes == 10_737_418_240
        assert metric.storage_usage_bytes == 5_368_709_120
        assert metric.ephemeral_storage_request_bytes == 1_073_741_824
        assert metric.ephemeral_storage_usage_bytes == 536_870_912

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
            network_receive_bytes=10_485_760,
            network_transmit_bytes=5_242_880,
            disk_read_bytes=20_971_520,
            disk_write_bytes=10_485_760,
            storage_request_bytes=10_737_418_240,
            storage_usage_bytes=5_368_709_120,
            ephemeral_storage_request_bytes=1_073_741_824,
            ephemeral_storage_usage_bytes=536_870_912,
            gpu_usage_millicores=500,
            restart_count=0,
            timestamp=ts,
            node="node-1",
            owner_kind="Deployment",
            owner_name="full-app",
        )
        assert metric.pod_name == "full-pod"
        assert metric.cpu_request == 1000
        assert metric.network_receive_bytes == 10_485_760
        assert metric.disk_write_bytes == 10_485_760
        assert metric.storage_request_bytes == 10_737_418_240
        assert metric.gpu_usage_millicores == 500
        assert metric.restart_count == 0

    def test_model_dump_includes_new_fields(self):
        """model_dump() should include all new fields."""
        metric = CombinedMetric(
            pod_name="test",
            namespace="default",
            network_receive_bytes=1024,
            disk_read_bytes=2048,
            restart_count=1,
        )
        data = metric.model_dump()
        assert "network_receive_bytes" in data
        assert "network_transmit_bytes" in data
        assert "disk_read_bytes" in data
        assert "disk_write_bytes" in data
        assert "storage_request_bytes" in data
        assert "storage_usage_bytes" in data
        assert "gpu_usage_millicores" in data
        assert "restart_count" in data
        assert "ephemeral_storage_request_bytes" in data
        assert "ephemeral_storage_usage_bytes" in data
