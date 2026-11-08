# tests/core/test_config_service.py
"""Runtime service-endpoint overrides are applied in memory only (roadmap M4)."""

import pytest

from greenkube.core.config import Config, ConfigService


def test_apply_reloads_settings_and_bumps_the_version(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://before:9090")
    settings = Config()
    service = ConfigService(settings)

    assert service.apply({"PROMETHEUS_URL": "http://after:9090"}) == 1
    assert settings.PROMETHEUS_URL == "http://after:9090"
    assert service.apply({"PROMETHEUS_URL": "http://again:9090"}) == 2


def test_apply_rejects_keys_outside_the_runtime_allowlist(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    service = ConfigService(Config())

    with pytest.raises(ValueError, match="API_KEY"):
        service.apply({"API_KEY": "attacker"})

    assert service.version == 0
