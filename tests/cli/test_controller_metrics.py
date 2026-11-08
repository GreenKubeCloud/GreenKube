"""Controller Prometheus endpoint (roadmap A12, BUG-14).

The controller runs collection, verification and, since E1, the recommendation
scans, so their control-loop metrics (``greenkube_optimization_runs_total`` and
friends) are recorded in the controller's process registry. The API process
serves only its own registry. The end-to-end tests below drive the real
``_async_start`` wiring, the real ``RecommendationScanService`` and the real
``OptimizationEngine`` (with fake sources) on a SQLite run queue, then scrape
the controller listener.
"""

import asyncio
import socket
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from greenkube.cli import start as start_module
from greenkube.cli.controller_metrics import (
    create_controller_metrics_app,
    create_controller_metrics_server,
    start_controller_metrics_server,
    stop_controller_metrics_server,
)
from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.observability import (
    REGISTRY,
    record_collection_request,
    record_collection_run,
    record_metric_provenance,
    record_verification,
)
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.scan_service import RecommendationScanService
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType
from greenkube.models.metrics import RecommendationSource as SourceName
from greenkube.models.optimization_run import OptimizationRunStatus
from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


def _configure_metrics_auth(monkeypatch, *, api_key: str, environment: str = "test"):
    config = SimpleNamespace(API_KEY=api_key, ENVIRONMENT=environment, API_AUTH_MODE="api_key")
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: config)
    monkeypatch.setattr("greenkube.core.config.config", config)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_controller_endpoint_exposes_controller_side_telemetry(monkeypatch):
    """Verification and collection telemetry recorded in the controller is scrapeable (BUG-14)."""
    _configure_metrics_auth(monkeypatch, api_key="controller-secret")
    record_verification("passed")
    record_collection_request("opencost", "error", 0.2)
    record_collection_run(3.5)
    record_metric_provenance(
        [
            CombinedMetric(pod_name="p", namespace="n", cost_source="opencost", intensity_source="zone_default"),
            CombinedMetric(pod_name="p", namespace="n", container_name="c", usage_source="prometheus"),
        ]
    )

    with TestClient(create_controller_metrics_app()) as client:
        assert client.get("/prometheus/metrics").status_code == 401
        assert client.get("/prometheus/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/docs", headers={"Authorization": "Bearer controller-secret"}).status_code == 404
        response = client.get("/prometheus/metrics", headers={"Authorization": "Bearer controller-secret"})

    assert response.status_code == 200
    body = response.text
    assert 'greenkube_verification_outcomes_total{outcome="passed"}' in body
    assert 'greenkube_collection_source_requests_total{outcome="error",source="opencost"}' in body
    assert "greenkube_collection_run_duration_seconds_count" in body
    assert 'greenkube_metric_rows_written_total{dimension="cost",scope="pod",source="opencost"}' in body
    assert 'greenkube_metric_rows_written_total{dimension="intensity",scope="pod",source="zone_default"}' in body
    assert 'greenkube_metric_rows_written_total{dimension="usage",scope="container",source="prometheus"}' in body


def test_controller_endpoint_serves_the_pod_network_when_the_controller_has_no_api_key(monkeypatch):
    """Helm keeps the API key out of the controller (least privilege); the endpoint stays scrapeable."""
    _configure_metrics_auth(monkeypatch, api_key="", environment="production")

    with TestClient(create_controller_metrics_app()) as client:
        response = client.get("/prometheus/metrics")
        assert client.get("/api/v1/health").status_code == 404

    assert response.status_code == 200


def test_controller_endpoint_also_answers_on_the_default_metrics_path(monkeypatch):
    """Scrapers that assume Prometheus' default ``/metrics`` path get the same registry, with the same auth."""
    _configure_metrics_auth(monkeypatch, api_key="controller-secret")
    record_collection_run(1.0)

    with TestClient(create_controller_metrics_app()) as client:
        assert client.get("/metrics").status_code == 401
        response = client.get("/metrics", headers={"Authorization": "Bearer controller-secret"})

    assert response.status_code == 200
    assert "greenkube_collection_run_duration_seconds_count" in response.text


def test_controller_metrics_port_must_be_valid():
    with pytest.raises(ValueError):
        create_controller_metrics_server(0)
    with pytest.raises(ValueError):
        create_controller_metrics_server(70_000)


async def test_disabled_port_starts_nothing():
    assert await start_controller_metrics_server(0) is None
    await stop_controller_metrics_server(None)


async def test_listener_serves_and_stops(monkeypatch):
    _configure_metrics_auth(monkeypatch, api_key="controller-secret")
    port = _free_port()

    handle = await start_controller_metrics_server(port, host="127.0.0.1")
    assert handle is not None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/prometheus/metrics",
                headers={"Authorization": "Bearer controller-secret"},
            )
        assert response.status_code == 200
    finally:
        await stop_controller_metrics_server(handle)
    assert handle.task.done()


async def test_bind_failure_does_not_stop_the_controller():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        handle = await start_controller_metrics_server(port, host="127.0.0.1", startup_timeout=5)

    assert handle is None


def test_provenance_mix_bounds_unknown_values():
    record_metric_provenance([CombinedMetric(pod_name="p", namespace="n", cost_source="something-new")])

    from greenkube.core.observability import generate_latest_observability

    assert 'source="something-new"' not in generate_latest_observability().decode()


# --- End-to-end: the controller serves the metrics its scans record (E1) ---

API_KEY = "controller-scrape-key"
BROKEN_SOURCE = "controller-metrics-broken-source"
SUCCEEDED = {"status": "succeeded"}


def _sample(name: str, labels: Optional[dict] = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _scraped(body: str, name: str, labels: Optional[dict] = None) -> float:
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            if sample.name == name and sample.labels == (labels or {}):
                return float(sample.value)
    return 0.0


class _Source(RecommendationSource):
    def __init__(self, name: str, recs: List[Recommendation], error: Optional[Exception] = None) -> None:
        self.name = name
        self._recs = recs
        self._error = error

    async def is_available(self) -> bool:
        return True

    async def collect(self, context):
        if self._error is not None:
            raise self._error
        return list(self._recs)


def _rec(pod: str) -> Recommendation:
    return Recommendation(
        pod_name=pod,
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="pod",
        description=f"Reduce {pod}",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=100,
        potential_savings_cost=10.0,
        source=SourceName("greenkube"),
    )


class _Elector:
    def __init__(self, is_leader: bool) -> None:
        self.is_leader = is_leader

    @property
    def fencing_token(self) -> Optional[int]:
        return 1 if self.is_leader else None

    async def try_acquire_or_renew(self) -> bool:
        return self.is_leader

    async def run(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()


@pytest.fixture
async def controller_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "controller.sqlite"))
    monkeypatch.setenv("GREENKUBE_API_KEY", API_KEY)
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        monkeypatch.delenv("GREENKUBE_API_KEY")
        config.reload()


def _install_controller(
    monkeypatch, manager: DatabaseManager, *, elector: Optional[_Elector], metrics_port: int
) -> None:
    runs = SQLiteOptimizationRunRepository(manager)
    recommendations = SQLiteRecommendationRepository(manager)

    def engine_factory(run_repository) -> OptimizationEngine:
        engine = OptimizationEngine(
            sources=[
                _Source("greenkube", [_rec("api")]),
                _Source(BROKEN_SOURCE, [], error=RuntimeError("analyzer crashed")),
            ],
            run_repository=run_repository,
        )
        engine.build_context = AsyncMock(return_value=OptimizationContext(config=get_config(), metrics=[]))
        return engine

    def build_scan_service(*, should_continue=None, identity=None):
        return RecommendationScanService(
            runs,
            combined_repo=AsyncMock(),
            node_repo=AsyncMock(),
            reco_repo=recommendations,
            interval=timedelta(hours=1),
            identity="controller-0",
            engine_factory=cast(Any, engine_factory),
            should_continue=should_continue,
        )

    db_manager = MagicMock()
    db_manager.connect = AsyncMock()
    processor = MagicMock()
    processor.close = AsyncMock()
    discovery = SimpleNamespace(run_periodic=AsyncMock())

    monkeypatch.setattr(
        start_module,
        "get_config",
        lambda: SimpleNamespace(
            LOG_LEVEL="INFO",
            LOG_FORMAT="console",
            CLUSTER_NAME="test-cluster",
            DB_TYPE="sqlite",
            PROMETHEUS_QUERY_RANGE_STEP="5m",
            NODE_ANALYSIS_INTERVAL="1h",
            RECOMMENDATION_LIFECYCLE_INTERVAL="5m",
            CONTROLLER_METRICS_PORT=metrics_port,
        ),
    )
    monkeypatch.setattr("greenkube.core.db.get_db_manager", lambda: db_manager)
    monkeypatch.setattr("greenkube.automation.plan_runtime.initialize_gitops_provider_configs", AsyncMock())
    monkeypatch.setattr(
        "greenkube.gitops.discovery.runtime.create_gitops_discovery_runtime", AsyncMock(return_value=discovery)
    )
    monkeypatch.setattr(start_module, "_start_gitops_sync_observer", AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, "_create_controller_leader_election", lambda cfg: elector)
    monkeypatch.setattr(start_module, "get_data_processor", lambda: processor)
    for name in (
        "collect_carbon_intensity_for_all_zones",
        "analyze_nodes",
        "async_write_combined_metrics_to_database",
        "attribute_recommendation_savings",
        "run_recommendation_lifecycle",
        "compress_metrics",
        "refresh_dashboard_summary",
    ):
        monkeypatch.setattr(start_module, name, AsyncMock())
    monkeypatch.setattr("greenkube.core.optimization.scan_service.RECOMMENDATION_SCAN_POLL_INTERVAL", "1s")
    monkeypatch.setattr(
        "greenkube.core.optimization.scan_service.create_recommendation_scan_service", build_scan_service
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)


async def _scrape(port: int, *, token: Optional[str] = API_KEY) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        return await client.get("/prometheus/metrics", headers=headers)


async def _wait_for(predicate, controller: asyncio.Task, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline and not controller.done():
        if await predicate():
            return
        await asyncio.sleep(0.05)
    if controller.done():
        await controller
    raise AssertionError("condition not reached before the timeout")


async def _stop(controller: asyncio.Task) -> None:
    controller.cancel()
    await asyncio.gather(controller, return_exceptions=True)


def _port_is_closed(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.parametrize("leader_election", [False, True], ids=["inline", "leader"])
async def test_controller_serves_the_optimization_run_metrics_its_scans_record(
    controller_db, monkeypatch, leader_election
):
    port = _free_port()
    _install_controller(
        monkeypatch, controller_db, elector=_Elector(True) if leader_election else None, metrics_port=port
    )
    runs = SQLiteOptimizationRunRepository(controller_db)
    succeeded_before = _sample("greenkube_optimization_runs_total", SUCCEEDED)
    generated_before = _sample("greenkube_recommendations_generated_total")
    failures_before = _sample("greenkube_optimization_analyzer_failures_total", {"source": BROKEN_SOURCE})
    durations_before = _sample("greenkube_optimization_run_duration_seconds_count")

    controller = asyncio.create_task(start_module._async_start(last=None))
    try:

        async def scan_finished() -> bool:
            stored = await runs.list_runs(limit=5)
            return bool(stored) and stored[0].status == OptimizationRunStatus.SUCCEEDED

        await _wait_for(scan_finished, controller)
        unauthenticated = await _scrape(port, token=None)
        response = await _scrape(port)
    finally:
        await _stop(controller)

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    body = response.text
    assert _scraped(body, "greenkube_optimization_runs_total", SUCCEEDED) == succeeded_before + 1
    assert _scraped(body, "greenkube_recommendations_generated_total") == generated_before + 1
    assert (
        _scraped(body, "greenkube_optimization_analyzer_failures_total", {"source": BROKEN_SOURCE})
        == failures_before + 1
    )
    assert _scraped(body, "greenkube_optimization_run_duration_seconds_count") == durations_before + 1
    assert _scraped(body, "greenkube_last_successful_optimization_run_timestamp_seconds") > 0
    assert "greenkube_recommendation_scan_resident_memory_bytes" in body
    assert _port_is_closed(port), "the listener must stop with the controller"


async def test_standby_controller_still_serves_its_registry_without_scanning(controller_db, monkeypatch):
    port = _free_port()
    _install_controller(monkeypatch, controller_db, elector=_Elector(False), metrics_port=port)
    runs = SQLiteOptimizationRunRepository(controller_db)

    controller = asyncio.create_task(start_module._async_start(last=None))
    try:

        async def serving() -> bool:
            try:
                return (await _scrape(port)).status_code == 200
            except httpx.TransportError:
                return False

        await _wait_for(serving, controller)
        await asyncio.sleep(1.2)
    finally:
        await _stop(controller)

    assert await runs.list_runs(limit=5) == []
    assert _port_is_closed(port)
