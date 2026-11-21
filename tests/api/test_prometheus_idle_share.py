"""The Prometheus endpoint exposes the node idle share and the cluster totals (WP-05.3, NRG-02).

The allocated series (``greenkube_cluster_*_total``, per namespace and per
pod) keep summing pod rows only; the idle share is a separate family and the
cluster total (allocated + idle) another, so no sum double counts. When the
idle history does not cover the scraped snapshot, the idle and cluster-total
series are absent (Prometheus has no null), never zero.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.api.metrics_endpoint import get_metrics_output, refresh_metrics_from_db
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric


def _sample(name: str) -> float | None:
    from prometheus_client.parser import text_string_to_metric_families

    for family in text_string_to_metric_families(get_metrics_output().decode()):
        for sample in family.samples:
            if sample.name == name:
                return sample.value
    return None


def _pod(name: str, node: str, ts: datetime) -> CombinedMetric:
    return CombinedMetric(
        pod_name=name,
        namespace="shop",
        timestamp=ts,
        duration_seconds=300,
        node=node,
        joules=1000.0,
        co2e_grams=2.0,
        embodied_co2e_grams=0.5,
        total_cost=0.1,
    )


def _idle(node: str, ts: datetime, joules: float = 4000.0) -> NodeIdleMetric:
    return NodeIdleMetric(
        node=node,
        timestamp=ts,
        duration_seconds=300,
        joules=joules,
        co2e_grams=8.0,
        embodied_co2e_grams=1.0,
        total_cost=0.3,
        cost_cpu=0.2,
        cost_ram=0.1,
        cost_source="model",
        cpu_unreserved_cores=1.0,
        memory_unreserved_bytes=1.0,
    )


def _repos(pods, idle_rows, since):
    combined = AsyncMock()
    combined.read_latest_per_pod = AsyncMock(return_value=pods)
    combined.read_idle_metrics = AsyncMock(return_value=idle_rows)
    combined.idle_data_since = AsyncMock(return_value=since)
    node_repo = AsyncMock()
    node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    reco_repo = AsyncMock()
    reco_repo.get_active_recommendations = AsyncMock(return_value=[])
    reco_repo.get_applied_recommendations_stats = AsyncMock(return_value=[])
    reco_repo.get_applied_recommendations = AsyncMock(return_value=[])
    return combined, node_repo, reco_repo


@pytest.mark.asyncio
async def test_idle_share_and_cluster_total_are_separate_gauges():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    ts = now - timedelta(minutes=5)
    pods = [_pod("a", "n1", ts), _pod("b", "n2", ts)]
    # Two steps of node n1: only its latest idle row counts, like the latest row per pod.
    idle_rows = [_idle("n1", ts - timedelta(minutes=5), joules=9999.0), _idle("n1", ts), _idle("n2", ts)]
    await refresh_metrics_from_db(*_repos(pods, idle_rows, since=ts - timedelta(days=1)))

    assert _sample("greenkube_cluster_energy_joules_total") == pytest.approx(2000.0)  # allocated, unchanged
    assert _sample("greenkube_cluster_idle_energy_joules") == pytest.approx(8000.0)
    assert _sample("greenkube_cluster_idle_co2e_grams") == pytest.approx(16.0)
    assert _sample("greenkube_cluster_idle_embodied_co2e_grams") == pytest.approx(2.0)
    assert _sample("greenkube_cluster_idle_cost_dollars") == pytest.approx(0.6)
    assert _sample("greenkube_cluster_with_idle_energy_joules") == pytest.approx(10000.0)
    assert _sample("greenkube_cluster_with_idle_co2e_grams") == pytest.approx(20.0)
    assert _sample("greenkube_cluster_with_idle_embodied_co2e_grams") == pytest.approx(3.0)
    assert _sample("greenkube_cluster_with_idle_cost_dollars") == pytest.approx(0.8)
    # The idle share never enters the per-namespace allocated series.
    assert _sample("greenkube_namespace_energy_joules_total") == pytest.approx(2000.0)


@pytest.mark.asyncio
async def test_short_idle_history_removes_the_idle_series():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    ts = now - timedelta(minutes=10)
    # A first scrape with a known idle share, then pods older than the first idle row.
    await refresh_metrics_from_db(*_repos([_pod("a", "n1", ts)], [_idle("n1", ts)], since=ts))
    assert _sample("greenkube_cluster_idle_energy_joules") == pytest.approx(4000.0)

    pods = [_pod("a", "n1", ts - timedelta(minutes=5)), _pod("b", "n1", ts)]
    await refresh_metrics_from_db(*_repos(pods, [_idle("n1", ts)], since=ts))
    assert _sample("greenkube_cluster_idle_energy_joules") is None
    assert _sample("greenkube_cluster_with_idle_energy_joules") is None
    assert _sample("greenkube_cluster_energy_joules_total") == pytest.approx(2000.0)


@pytest.mark.asyncio
async def test_no_idle_row_removes_the_idle_series():
    ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    await refresh_metrics_from_db(*_repos([_pod("a", "n1", ts)], [], since=None))
    assert _sample("greenkube_cluster_idle_cost_dollars") is None
    assert _sample("greenkube_cluster_with_idle_cost_dollars") is None
