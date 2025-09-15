# tests/core/test_processor.py
import unittest
from unittest.mock import MagicMock
from src.greenkube.core.processor import DataProcessor
from src.greenkube.core.calculator import CarbonCalculator
from src.greenkube.models.metrics import EnergyMetric, CostMetric
from datetime import datetime, timezone

class TestDataProcessor(unittest.TestCase):
    """
    Test suite for the DataProcessor.
    """

    def test_run_pipeline_combines_data_correctly(self):
        """
        Tests que le processeur combine correctement les données.
        """
        # Arrange
        energy_data = [EnergyMetric(pod_name="pod-a", namespace="ns1", joules=3600000.0, timestamp=datetime.now(timezone.utc))]
        cost_data = [CostMetric(pod_name="pod-a", namespace="ns1", total_cost=0.3, timestamp=datetime.now(timezone.utc), cpu_cost=0.1, ram_cost=0.2)]

        mock_energy_collector = MagicMock()
        mock_energy_collector.collect.return_value = energy_data
        mock_cost_collector = MagicMock()
        mock_cost_collector.collect.return_value = cost_data

        # Créer un mock repository et le calculateur
        mock_repo = MagicMock()
        mock_repo.get_latest_for_zone.return_value = 100.0
        calculator = CarbonCalculator(repository=mock_repo)

        # Injecter les mocks dans le processeur
        processor = DataProcessor(
            energy_collector=mock_energy_collector,
            cost_collector=mock_cost_collector,
            calculator=calculator
        )

        # Act
        results = processor.run()

        # Assert
        self.assertEqual(len(results), 1)
        pod_a = results[0]
        self.assertEqual(pod_a.total_cost, 0.3)
        # (1 kWh * 1.5 PUE) * 100 g/kWh = 150 grams
        self.assertAlmostEqual(pod_a.co2e_grams, 150.0)
