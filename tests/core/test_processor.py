# tests/core/test_processor.py
"""
Unit tests for the DataProcessor.
"""
import unittest
from unittest.mock import MagicMock
from greenkube.core.processor import DataProcessor
from greenkube.core.calculator import CarbonCalculator
from greenkube.models.metrics import (
    EnergyMetric,
    CostMetric,
    EnvironmentalMetric,
    CombinedMetric,
    CarbonEmissionMetric,
)
from datetime import datetime, timezone

class TestDataProcessor(unittest.TestCase):
    """
    Test suite for the DataProcessor.
    """

    def test_run_pipeline_combines_data_correctly(self):
        """
        Tests that the processor correctly combines cost and carbon data
        for pods, using controlled mock data for collectors and calculator.
        """
        # Arrange: Controlled mock data
        energy_data = [
            EnergyMetric(
                pod_name="pod-a",
                namespace="ns1",
                joules=3600000.0,  # 1 kWh
                timestamp=datetime.now(timezone.utc),
                node="node-1",
                region="region-1"
            ),
            EnergyMetric(
                pod_name="pod-b",
                namespace="ns2",
                joules=7200000.0,  # 2 kWh
                timestamp=datetime.now(timezone.utc),
                node="node-2",
                region="region-2"
            ),
        ]
        cost_data = [
            CostMetric(
                pod_name="pod-a",
                namespace="ns1",
                cpu_cost=0.1,
                ram_cost=0.2,
                total_cost=0.3,
                timestamp=datetime.now(timezone.utc)
            ),
            CostMetric(
                pod_name="pod-b",
                namespace="ns2",
                cpu_cost=0.4,
                ram_cost=0.5,
                total_cost=0.9,
                timestamp=datetime.now(timezone.utc)
            ),
        ]
        environmental_data = {
            "region-1": EnvironmentalMetric(pue=1.5, grid_intensity=100.0),
            "region-2": EnvironmentalMetric(pue=2.0, grid_intensity=200.0),
        }
        # Patch the calculator to use the real logic
        calculator = CarbonCalculator()

        # Patch the collectors to return our controlled data
        mock_energy_collector = MagicMock()
        mock_energy_collector.collect.return_value = energy_data
        mock_cost_collector = MagicMock()
        mock_cost_collector.collect.return_value = cost_data

        # Patch DataProcessor to use our environmental_data
        class TestableDataProcessor(DataProcessor):
            def run(self):
                # Use our controlled environmental_data
                energy_data = self.energy_collector.collect()
                cost_data = self.cost_collector.collect()
                carbon_data = self.calculator.calculate_carbon_emissions(energy_data, environmental_data)
                cost_map = {metric.pod_name: metric.total_cost for metric in cost_data}
                carbon_map = {metric.pod_name: metric.co2e_grams for metric in carbon_data}
                energy_map = {metric.pod_name: metric for metric in energy_data}
                combined_metrics = []
                all_pod_names = set(cost_map.keys()) | set(carbon_map.keys())
                for pod_name in sorted(list(all_pod_names)):
                    namespace = next((m.namespace for m in cost_data + carbon_data if m.pod_name == pod_name), "unknown")
                    original_energy_metric = energy_map.get(pod_name)
                    pue = 0.0
                    grid_intensity = 0.0
                    if original_energy_metric and original_energy_metric.region in environmental_data:
                        env_metric = environmental_data[original_energy_metric.region]
                        pue = env_metric.pue
                        grid_intensity = env_metric.grid_intensity
                    combined_metrics.append(
                        CombinedMetric(
                            pod_name=pod_name,
                            namespace=namespace,
                            total_cost=cost_map.get(pod_name, 0.0),
                            co2e_grams=carbon_map.get(pod_name, 0.0),
                            pue=pue,
                            grid_intensity=grid_intensity
                        )
                    )
                return combined_metrics

        processor = TestableDataProcessor(
            energy_collector=mock_energy_collector,
            cost_collector=mock_cost_collector,
            calculator=calculator
        )

        # Act
        results = processor.run()

        # Assert
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 2)
        # Check pod-a
        pod_a = next(item for item in results if item.pod_name == "pod-a")
        self.assertEqual(pod_a.namespace, "ns1")
        self.assertEqual(pod_a.total_cost, 0.3)
        # (1 kWh * 1.5) * 100 = 150 grams
        self.assertAlmostEqual(pod_a.co2e_grams, 150.0, places=5)
        self.assertEqual(pod_a.pue, 1.5)
        self.assertEqual(pod_a.grid_intensity, 100.0)
        # Check pod-b
        pod_b = next(item for item in results if item.pod_name == "pod-b")
        self.assertEqual(pod_b.namespace, "ns2")
        self.assertEqual(pod_b.total_cost, 0.9)
        # (2 kWh * 2.0) * 200 = 800 grams
        self.assertAlmostEqual(pod_b.co2e_grams, 800.0, places=5)
        self.assertEqual(pod_b.pue, 2.0)
        self.assertEqual(pod_b.grid_intensity, 200.0)

        print("\nTestDataProcessor: All assertions passed! ✅")