from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import config
from greenkube.storage.base_repository import CarbonIntensityRepository


class DummyRepo(CarbonIntensityRepository):
    def get_for_zone_at_time(self, zone, ts):
        return None

    def save_history(self, records, zone=None):
        return 0


def test_calculator_uses_config_default_pue(monkeypatch):
    # Ensure CLOUD_PROVIDER affects config.DEFAULT_PUE and calculator picks it
    monkeypatch.setenv("CLOUD_PROVIDER", "ovh")
    import importlib as _importlib

    _importlib.reload(__import__("greenkube.core.config", fromlist=["config"]))
    # Instantiate calculator without pue - should use config.DEFAULT_PUE
    calc = CarbonCalculator(repository=DummyRepo())
    assert calc.pue == config.DEFAULT_PUE


def test_calculator_respects_explicit_pue():
    # Explicit pue parameter should override config
    calc = CarbonCalculator(repository=DummyRepo(), pue=1.42)
    assert calc.pue == 1.42
