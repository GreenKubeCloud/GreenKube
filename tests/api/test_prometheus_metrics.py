# tests/api/test_prometheus_metrics.py
"""The Prometheus exposition: operational metrics and the ledger savings counters only (B.7).

Footprint data (cost, CO2e, energy per pod, namespace and node) stays in the
API and the Footprint pages; no API series carries a ``pod`` label, and
``namespace`` appears only on the bounded control-loop families.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_combined_metrics_repository, get_gitops_binding_repository
from greenkube.api.metrics_endpoint import REGISTRY, refresh_metrics_from_db
from greenkube.models.metrics import CombinedMetric
from greenkube.models.savings import SavingsCounterTotal
from greenkube.storage.base_gitops_repository import AutomationBacklogSummary

#: Every family of the API registry. Adding one is a Prometheus cost decision: update docs/prometheus-grafana.md.
API_FAMILIES = {
    "greenkube_last_collection_timestamp_seconds",
    "greenkube_metrics_total",
    "greenkube_co2e_savings_attributed_grams",
    "greenkube_cost_savings_attributed_dollars",
    "greenkube_co2e_savings_reversed_grams",
    "greenkube_cost_savings_reversed_dollars",
    "greenkube_savings_measured_vs_projected_ratio",
}


def _families(output: str):
    return list(text_string_to_metric_families(output))


def _sample(output: str, name: str, **labels: str) -> float | None:
    for family in _families(output):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


def _pod(name: str, namespace: str, ts: datetime) -> CombinedMetric:
    return CombinedMetric(pod_name=name, namespace=namespace, timestamp=ts, node="node-1", total_cost=0.1)


@pytest.fixture
def mock_combined_metrics_repo():
    repo = AsyncMock()
    repo.read_latest_per_pod = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_gitops_repo():
    repo = AsyncMock()
    repo.get_automation_backlog_summary = AsyncMock(return_value=AutomationBacklogSummary())
    return repo


@pytest.fixture
def client(mock_combined_metrics_repo, mock_gitops_repo):
    app = create_app()
    app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
    app.dependency_overrides[get_gitops_binding_repository] = lambda: mock_gitops_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_prometheus_text(self, client):
        response = client.get("/prometheus/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers.get("content-type", "")

    def test_metrics_endpoint_exposes_control_loop_gitops_telemetry(self, client):
        from greenkube.core.observability import record_sync_outcome

        record_sync_outcome("synced")
        response = client.get("/prometheus/metrics")

        assert response.status_code == 200
        assert 'greenkube_gitops_sync_outcomes_total{outcome="synced"}' in response.text
        assert 'greenkube_automation_backlog{queue="operations",state="queued"} 0.0' in response.text

    def test_no_series_carries_a_pod_label(self, client, mock_combined_metrics_repo):
        ts = datetime.now(timezone.utc) - timedelta(minutes=5)
        mock_combined_metrics_repo.read_latest_per_pod.return_value = [_pod("a", "shop", ts), _pod("b", "web", ts)]

        response = client.get("/prometheus/metrics")

        samples = [s for family in _families(response.text) for s in family.samples]
        assert samples
        assert not [s for s in samples if "pod" in s.labels]
        assert "greenkube_pod_" not in response.text
        assert "greenkube_namespace_" not in response.text
        assert "greenkube_sustainability" not in response.text


def test_api_registry_exposes_only_operational_and_savings_families():
    assert {family.name for family in REGISTRY.collect()} == API_FAMILIES


class TestCollectionFreshness:
    @pytest.mark.asyncio
    async def test_latest_timestamp_and_pod_count_come_from_the_latest_row_per_pod(self, mock_combined_metrics_repo):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        older, newer = now - timedelta(minutes=10), now - timedelta(minutes=5)
        pods = [_pod("a", "shop", older), _pod("b", "shop", newer)]
        mock_combined_metrics_repo.read_latest_per_pod.return_value = pods

        await refresh_metrics_from_db(mock_combined_metrics_repo)

        output = generate_latest(REGISTRY).decode()
        assert _sample(output, "greenkube_last_collection_timestamp_seconds") == newer.timestamp()
        assert _sample(output, "greenkube_metrics_total") == 2.0
        call = mock_combined_metrics_repo.read_latest_per_pod.await_args
        assert call.kwargs["end_time"] - call.kwargs["start_time"] == timedelta(minutes=20)

    @pytest.mark.asyncio
    async def test_stalled_collection_keeps_the_last_timestamp(self, mock_combined_metrics_repo):
        ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
        mock_combined_metrics_repo.read_latest_per_pod.return_value = [_pod("a", "shop", ts)]
        await refresh_metrics_from_db(mock_combined_metrics_repo)

        mock_combined_metrics_repo.read_latest_per_pod.return_value = []
        await refresh_metrics_from_db(mock_combined_metrics_repo)

        output = generate_latest(REGISTRY).decode()
        assert _sample(output, "greenkube_last_collection_timestamp_seconds") == ts.timestamp()
        assert _sample(output, "greenkube_metrics_total") == 0.0

    @pytest.mark.asyncio
    async def test_database_errors_do_not_fail_the_scrape(self, mock_combined_metrics_repo, mock_gitops_repo):
        mock_combined_metrics_repo.read_latest_per_pod.side_effect = RuntimeError("database down")
        savings_repo = AsyncMock()
        savings_repo.get_lifetime_counters = AsyncMock(side_effect=RuntimeError("database down"))
        mock_gitops_repo.get_automation_backlog_summary.side_effect = RuntimeError("database down")

        await refresh_metrics_from_db(
            mock_combined_metrics_repo, savings_repo=savings_repo, gitops_repository=mock_gitops_repo
        )


class TestSavingsLedgerCounters:
    @pytest.mark.asyncio
    async def test_savings_ledger_counters_are_exposed_as_monotonic_counters(self, mock_combined_metrics_repo):
        """C0.14: *_total savings series are counters, split by method, with separate reversals."""
        savings_repo = AsyncMock()
        savings_repo.get_lifetime_counters = AsyncMock(
            return_value=[
                SavingsCounterTotal(
                    recommendation_type="RIGHTSIZING_CPU",
                    method="measured",
                    accrued_co2e_grams=30.0,
                    accrued_cost_dollars=3.0,
                    reversed_co2e_grams=10.0,
                    reversed_cost_dollars=1.0,
                ),
                SavingsCounterTotal(recommendation_type="RIGHTSIZING_CPU", method="projected", accrued_co2e_grams=60.0),
            ]
        )

        await refresh_metrics_from_db(mock_combined_metrics_repo, savings_repo=savings_repo)

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "# TYPE greenkube_co2e_savings_attributed_grams_total counter" in output
        assert "# TYPE greenkube_co2e_savings_reversed_grams_total counter" in output
        labels = {"method": "measured", "recommendation_type": "RIGHTSIZING_CPU"}
        assert _sample(output, "greenkube_co2e_savings_attributed_grams_total", **labels) == 30.0
        assert _sample(output, "greenkube_co2e_savings_reversed_grams_total", **labels) == 10.0
        assert _sample(output, "greenkube_cost_savings_attributed_dollars_total", **labels) == 3.0
        assert _sample(output, "greenkube_savings_measured_vs_projected_ratio") == 0.25

    @pytest.mark.asyncio
    async def test_savings_refresh_is_throttled_between_scrapes(self, mock_combined_metrics_repo, monkeypatch):
        """Scrapes within the TTL reuse the savings counters instead of rescanning the ledger."""
        from greenkube.api import metrics_endpoint

        clock = {"now": 1000.0}
        monkeypatch.setattr(metrics_endpoint.time, "monotonic", lambda: clock["now"])
        savings_repo = AsyncMock()
        savings_repo.get_lifetime_counters = AsyncMock(side_effect=[RuntimeError("down"), [], []])

        async def scrape():
            await refresh_metrics_from_db(mock_combined_metrics_repo, savings_repo=savings_repo)

        await scrape()  # the ledger read fails: nothing is cached
        await scrape()  # retried, succeeds and is cached
        clock["now"] += metrics_endpoint.SAVINGS_REFRESH_TTL_SECONDS - 1
        await scrape()  # within the TTL: the ledger is not read
        assert savings_repo.get_lifetime_counters.await_count == 2
        clock["now"] += 1
        await scrape()
        assert savings_repo.get_lifetime_counters.await_count == 3


class TestClusterNameConfig:
    """CLUSTER_NAME must be a configurable env var."""

    def test_cluster_name_defaults_to_empty(self, monkeypatch):
        from unittest.mock import patch

        from greenkube.core.config import Config

        with patch.object(Config, "_auto_detect_cluster_name", return_value=""):
            monkeypatch.delenv("CLUSTER_NAME", raising=False)
            cfg = Config()
        assert cfg.CLUSTER_NAME == ""

    def test_cluster_name_from_env(self, monkeypatch):
        monkeypatch.setenv("CLUSTER_NAME", "prod-eu-west")
        from greenkube.core.config import Config

        cfg = Config()
        assert cfg.CLUSTER_NAME == "prod-eu-west"
