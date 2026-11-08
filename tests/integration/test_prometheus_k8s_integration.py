# tests/integration/test_prometheus_k8s_integration.py

from datetime import datetime, timezone

import pytest

from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import config


class DummyConfig:
    PROMETHEUS_URL = "http://mock-prometheus:9090"
    PROMETHEUS_QUERY_RANGE_STEP = "5m"


@pytest.fixture
def dummy_config():
    return DummyConfig()


@pytest.mark.asyncio
async def test_normalization_day_and_none(monkeypatch, dummy_config):
    """Test normalization granularity 'day' and 'none' behaviors."""
    from greenkube.core.config import config as core_config

    # Prepare a single energy metric
    from greenkube.models.metrics import EnergyMetric

    sample_dt = datetime.now(timezone.utc).replace(hour=10, minute=23, second=12, microsecond=0)
    _energy_metric = EnergyMetric(
        pod_name="p1",
        namespace="default",
        joules=3.6e6,
        timestamp=sample_dt.isoformat(),
        node="node-1",
    )

    class DummyRepoDay:
        async def get_for_zone_at_time(self, zone, timestamp):
            # For 'day' we expect midnight normalization
            assert timestamp.endswith("00:00:00+00:00") or "T00:00:00" in timestamp
            return 200.0

    # Day granularity (use monkeypatch to avoid global state leakage)
    monkeypatch.setattr(core_config, "NORMALIZATION_GRANULARITY", "day")
    calc_day = CarbonCalculator(repository=DummyRepoDay(), pue=config.DEFAULT_PUE, config=core_config)  # type: ignore[arg-type]
    # Directly call calculate_emissions
    res = await calc_day.calculate_emissions(3.6e6, "FR", sample_dt.isoformat())
    assert res is not None
    assert res.grid_intensity == 200.0

    # None granularity (no normalization) should call repository with exact timestamp
    class DummyRepoNone:
        async def get_for_zone_at_time(self, zone, timestamp):
            # Expect same hour minute second as sample_dt
            assert timestamp.startswith(sample_dt.replace(tzinfo=timezone.utc).isoformat()[:13])
            return 250.0

    monkeypatch.setattr(core_config, "NORMALIZATION_GRANULARITY", "none")
    calc_none = CarbonCalculator(repository=DummyRepoNone(), pue=config.DEFAULT_PUE, config=core_config)  # type: ignore[arg-type]
    res2 = await calc_none.calculate_emissions(3.6e6, "FR", sample_dt.isoformat())
    assert res2 is not None
    assert res2.grid_intensity == 250.0
