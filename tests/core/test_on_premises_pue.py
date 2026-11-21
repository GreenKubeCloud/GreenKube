"""F-10: ``cloudProvider: on-premise`` maps explicitly to the operator's PUE (no warning)."""

import logging

import pytest

from greenkube.core.config import Config


@pytest.mark.parametrize("provider", ["on-prem", "on-premise", "on-premises", "onprem", "On-Premise"])
def test_on_premises_provider_uses_the_configured_pue_without_warning(provider, caplog):
    with caplog.at_level(logging.INFO, logger="greenkube.core.config"):
        cfg = Config(CLOUD_PROVIDER=provider, DEFAULT_PUE=1.42, CLUSTER_NAME="test")

    assert cfg.DEFAULT_PUE == 1.42
    assert cfg.get_pue_for_provider(provider) == 1.42
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "CLOUD_PROVIDER" in r.getMessage()]
    assert any("on-premises" in r.getMessage() and "1.42" in r.getMessage() for r in caplog.records)


def test_unknown_provider_still_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="greenkube.core.config"):
        Config(CLOUD_PROVIDER="made-up-cloud", CLUSTER_NAME="test")

    assert any("Unknown CLOUD_PROVIDER" in r.getMessage() for r in caplog.records)
