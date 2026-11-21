"""F-10: the default-zone fallback is logged once per process and node, not at every collection."""

import logging
from unittest.mock import MagicMock

import pytest

from greenkube.core import node_zone_mapper
from greenkube.core.config import Config
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.models.node import NodeInfo

LOGGER = "greenkube.core.node_zone_mapper"


@pytest.fixture(autouse=True)
def _fresh_process_state():
    node_zone_mapper.reset_default_zone_log_state()
    yield
    node_zone_mapper.reset_default_zone_log_state()


def _nodes():
    return {
        name: NodeInfo(name=name, zone=None, region=None, cloud_provider="unknown", instance_type="bare")
        for name in ("node-a", "node-b")
    }


def _fallback_records(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.INFO and "default" in r.getMessage()]


@pytest.mark.asyncio
async def test_configured_default_zone_is_logged_once_per_node_at_info(caplog):
    mapper = NodeZoneMapper(MagicMock(), config=Config(DEFAULT_ZONE="FR", CLUSTER_NAME="test"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for _ in range(3):
            contexts = await mapper.map_nodes(_nodes())

    records = _fallback_records(caplog)
    assert len(records) == 2
    assert all(r.levelno == logging.INFO for r in records)
    # Every row still records that the zone is the configured default.
    assert contexts["node-a"].emaps_zone == "FR"
    assert contexts["node-a"].is_estimated
    assert "Used default zone 'FR'" in contexts["node-a"].estimation_reasons[0]


@pytest.mark.asyncio
async def test_unset_default_zone_warns_once_per_node(caplog):
    mapper = NodeZoneMapper(MagicMock(), config=Config(DEFAULT_ZONE="unknown", CLUSTER_NAME="test"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for _ in range(3):
            await mapper.map_nodes(_nodes())

    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_default_zone_log_state_is_bounded(monkeypatch):
    """Node churn cannot grow the once-per-node log state without bound."""
    monkeypatch.setattr(node_zone_mapper, "MAX_DEFAULT_ZONE_LOGGED_NODES", 3, raising=False)
    mapper = NodeZoneMapper(MagicMock(), config=Config(DEFAULT_ZONE="FR", CLUSTER_NAME="test"))

    for index in range(10):
        name = f"node-{index}"
        await mapper.map_nodes({name: NodeInfo(name=name, zone=None, region=None, cloud_provider="unknown")})
        assert len(node_zone_mapper._default_zone_logged) <= 3
