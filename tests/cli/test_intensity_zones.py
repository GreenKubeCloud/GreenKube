"""The hourly carbon intensity job fetches every zone rows are computed for (NRG-03).

Rows of a node whose zone and region cannot be mapped use ``DEFAULT_ZONE``; the job used to
fetch only mapped zones, so on-prem, kind, k3s and kubeadm clusters never got live intensity,
and it returned before the 24 h recompute when no zone mapped.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

from greenkube.cli import start as start_module
from greenkube.core.config import Config
from greenkube.models.node import NodeInfo


async def _run_intensity_job(nodes: dict, default_zone: str):
    repository = MagicMock()
    repository.save_history = AsyncMock(return_value=1)
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(return_value=nodes)
    node_collector.close = AsyncMock()
    em_collector = MagicMock()
    em_collector.collect = AsyncMock(return_value=[{"datetime": "2026-11-16T12:00:00Z", "carbonIntensity": 50}])
    em_collector.close = AsyncMock()
    combined_repo = MagicMock()
    combined_repo.recompute_carbon_with_latest_intensities = AsyncMock(return_value=0)
    with patch.dict(os.environ, {"DEFAULT_ZONE": default_zone}):
        cfg = Config()
    with (
        patch("greenkube.cli.start.get_config", return_value=cfg),
        patch("greenkube.cli.start.get_repository", return_value=repository),
        patch("greenkube.cli.start.get_combined_metrics_repository", return_value=combined_repo),
        patch("greenkube.cli.start.NodeCollector", return_value=node_collector),
        patch("greenkube.cli.start.get_electricity_provider", return_value=em_collector),
    ):
        await start_module.collect_carbon_intensity_for_all_zones()
    zones = {call.kwargs["zone"] for call in em_collector.collect.await_args_list}
    return zones, em_collector, repository, combined_repo


async def test_unlabelled_node_gets_its_default_zone_fetched():
    """sim_zone.py: a minikube node without zone labels and DEFAULT_ZONE=FR (acceptance a)."""
    nodes = {"minikube": NodeInfo(name="minikube", cloud_provider="unknown", instance_type="cpu-4")}
    zones, em_collector, repository, combined_repo = await _run_intensity_job(nodes, "FR")
    assert zones == {"FR"}
    assert em_collector.collect.await_count == 1
    repository.save_history.assert_awaited_once()
    combined_repo.recompute_carbon_with_latest_intensities.assert_awaited_once()


async def test_mixed_cluster_fetches_mapped_zones_and_the_default_zone():
    """Node A in eu-west-3a, node B unlabelled, DEFAULT_ZONE=DE (acceptance b)."""
    nodes = {
        "a": NodeInfo(name="a", zone="eu-west-3a", region="eu-west-3", cloud_provider="aws"),
        "b": NodeInfo(name="b", cloud_provider="unknown"),
    }
    zones, *_ = await _run_intensity_job(nodes, "DE")
    assert zones == {"FR", "DE"}


async def test_no_zone_at_all_makes_no_provider_call_but_still_recomputes():
    """DEFAULT_ZONE=unknown and no labels (acceptance c)."""
    nodes = {"minikube": NodeInfo(name="minikube", cloud_provider="unknown")}
    zones, em_collector, _, combined_repo = await _run_intensity_job(nodes, "unknown")
    assert zones == set()
    em_collector.collect.assert_not_awaited()
    combined_repo.recompute_carbon_with_latest_intensities.assert_awaited_once()


async def test_empty_node_list_fetches_the_default_zone_and_recomputes():
    zones, _, _, combined_repo = await _run_intensity_job({}, "FR")
    assert zones == {"FR"}
    combined_repo.recompute_carbon_with_latest_intensities.assert_awaited_once()
