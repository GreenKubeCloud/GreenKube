# tests/core/test_calculator_async_lock.py
"""
Tests that CarbonCalculator uses asyncio.Lock for proper async context safety.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import config


@pytest.mark.asyncio
async def test_calculator_uses_asyncio_lock():
    """Verify that the calculator uses asyncio.Lock instead of threading.Lock."""
    mock_repo = MagicMock()
    mock_repo.get_for_zone_at_time = AsyncMock(return_value=100.0)

    calculator = CarbonCalculator(repository=mock_repo, pue=config.DEFAULT_PUE)

    assert isinstance(calculator._lock, asyncio.Lock), (
        "CarbonCalculator should use asyncio.Lock for async-safe caching."
    )


@pytest.mark.asyncio
async def test_calculator_concurrent_async_access():
    """Ensure that concurrent async calls are handled correctly with asyncio.Lock."""
    mock_repo = MagicMock()
    call_count = 0

    async def slow_get(zone, ts):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return 100.0

    mock_repo.get_for_zone_at_time = slow_get

    calculator = CarbonCalculator(repository=mock_repo, pue=config.DEFAULT_PUE)

    # Fire multiple concurrent requests for the same zone+timestamp
    tasks = [
        calculator.calculate_emissions(joules=100.0, zone="FR", timestamp="2025-10-31T21:00:00Z") for _ in range(10)
    ]
    results = await asyncio.gather(*tasks)

    # All results should be valid
    assert all(r is not None for r in results)
    assert all(r.co2e_grams > 0 for r in results)

    # Repository should be called only once due to caching
    assert call_count == 1, f"Expected 1 repository call (cached), got {call_count}"


@pytest.mark.asyncio
async def test_clear_cache_works_with_asyncio_lock():
    """Verify clear_cache works correctly with asyncio.Lock."""
    mock_repo = MagicMock()
    mock_repo.get_for_zone_at_time = AsyncMock(return_value=100.0)

    calculator = CarbonCalculator(repository=mock_repo, pue=config.DEFAULT_PUE)

    # First call populates cache
    await calculator.calculate_emissions(joules=100.0, zone="FR", timestamp="2025-10-31T21:00:00Z")
    assert mock_repo.get_for_zone_at_time.call_count == 1

    # Clear cache
    await calculator.clear_cache()

    # Second call should hit the repository again
    await calculator.calculate_emissions(joules=100.0, zone="FR", timestamp="2025-10-31T21:00:00Z")
    assert mock_repo.get_for_zone_at_time.call_count == 2
