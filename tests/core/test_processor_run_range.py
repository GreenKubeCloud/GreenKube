import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from greenkube.core.processor import DataProcessor
from greenkube.models.metrics import CombinedMetric


class TestProcessorRunRange(unittest.TestCase):
    def test_run_range_uses_repository(self):
        # Arrange
        mock_repo = MagicMock()
        mock_repo.read_combined_metrics.return_value = [
            CombinedMetric(
                pod_name="test-pod",
                namespace="default",
                timestamp=datetime.now(timezone.utc),
                total_cost=0,
                co2e_grams=0,
                pue=1,
                grid_intensity=0,
                joules=0,
                cpu_request=0,
                memory_request=0,
            )
        ]

        processor = DataProcessor(
            prometheus_collector=MagicMock(),
            opencost_collector=MagicMock(),
            node_collector=MagicMock(),
            pod_collector=MagicMock(),
            electricity_maps_collector=MagicMock(),
            repository=mock_repo,
            calculator=MagicMock(),
            estimator=MagicMock(),
        )

        start = datetime(2023, 10, 23, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2023, 10, 24, 0, 0, 0, tzinfo=timezone.utc)

        # Act
        metrics = processor.run_range(start, end)

        # Assert
        mock_repo.read_combined_metrics.assert_called_once_with(start, end)
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].pod_name, "test-pod")
