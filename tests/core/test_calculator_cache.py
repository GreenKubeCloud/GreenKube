# tests/core/test_calculator.py

from unittest.mock import MagicMock
from src.greenkube.core.calculator import CarbonCalculator
from src.greenkube.core.config import config


def test_calculator_caches_intensity_per_run():
    # Arrange
    mock_repo = MagicMock()
    test_zone = "FR"
    test_ts = "2025-10-31T21:00:00Z"
    mock_repo.get_for_zone_at_time.return_value = 100.0

    calculator = CarbonCalculator(repository=mock_repo, pue=config.DEFAULT_PUE)

    # Act: call calculate_emissions multiple times with same zone+timestamp
    for _ in range(5):
        calculator.calculate_emissions(joules=100.0, zone=test_zone, timestamp=test_ts)

    # Assert: repository called only once due to caching
    mock_repo.get_for_zone_at_time.assert_called_once_with(test_zone, test_ts)
