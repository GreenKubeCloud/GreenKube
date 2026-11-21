import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from greenkube.cli import start as start_module
from greenkube.models.node import NodeInfo


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_saves_mapped_zones():
    repository = MagicMock()
    repository.save_history = AsyncMock(return_value=2)

    node_collector = MagicMock()
    node_collector.collect = AsyncMock(
        return_value={
            "node-a": NodeInfo(name="node-a", zone="eu-west-3a", region="eu-west-3", cloud_provider="aws"),
            "node-b": NodeInfo(name="node-b", zone=None, region="eu-west-3", cloud_provider="aws"),
        }
    )
    node_collector.close = AsyncMock()

    em_collector = MagicMock()
    em_collector.collect = AsyncMock(return_value=[{"datetime": "2026-04-30T12:00:00Z", "carbonIntensity": 50}])
    em_collector.close = AsyncMock()

    combined_repo = MagicMock()
    combined_repo.recompute_carbon_with_latest_intensities = AsyncMock(return_value=3)

    with patch("greenkube.cli.start.get_repository", return_value=repository):
        with patch("greenkube.cli.start.get_combined_metrics_repository", return_value=combined_repo):
            with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
                with patch("greenkube.cli.start.get_electricity_provider", return_value=em_collector):
                    with patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone", return_value="FR"):
                        await start_module.collect_carbon_intensity_for_all_zones()

    repository.save_history.assert_awaited_once()
    assert repository.save_history.await_args is not None
    assert repository.save_history.await_args.kwargs == {"zone": "FR"}
    node_collector.close.assert_awaited_once()
    em_collector.close.assert_awaited_once()

    # After saving the refreshed history, combined metrics are recomputed over
    # a ~24h window so provisional intensities are replaced by consolidated ones.
    combined_repo.recompute_carbon_with_latest_intensities.assert_awaited_once()
    rec_args = combined_repo.recompute_carbon_with_latest_intensities.await_args
    assert rec_args is not None
    assert rec_args.args[0] is repository
    window_hours = (rec_args.args[2] - rec_args.args[1]).total_seconds() / 3600
    assert 23.9 < window_hours <= 24


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_handles_initialization_failure():
    with patch("greenkube.cli.start.get_repository", side_effect=RuntimeError("db missing")):
        await start_module.collect_carbon_intensity_for_all_zones()


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_handles_no_nodes():
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(return_value={})
    node_collector.close = AsyncMock()
    em_collector = MagicMock()
    em_collector.close = AsyncMock()

    combined_repo = MagicMock()
    combined_repo.recompute_carbon_with_latest_intensities = AsyncMock(return_value=0)

    with patch("greenkube.cli.start.get_repository", return_value=MagicMock()):
        with patch("greenkube.cli.start.get_combined_metrics_repository", return_value=combined_repo):
            with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
                with patch("greenkube.cli.start.get_electricity_provider", return_value=em_collector):
                    await start_module.collect_carbon_intensity_for_all_zones()

    node_collector.close.assert_awaited_once()
    em_collector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_handles_unmapped_zones():
    repository = MagicMock()
    repository.save_history = AsyncMock()
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(
        return_value={"node-a": NodeInfo(name="node-a", zone="moon-1", region="moon", cloud_provider="test")}
    )
    node_collector.close = AsyncMock()
    em_collector = MagicMock()
    em_collector.close = AsyncMock()

    combined_repo = MagicMock()
    combined_repo.recompute_carbon_with_latest_intensities = AsyncMock(return_value=0)

    with patch("greenkube.cli.start.get_repository", return_value=repository):
        with patch("greenkube.cli.start.get_combined_metrics_repository", return_value=combined_repo):
            with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
                with patch("greenkube.cli.start.get_electricity_provider", return_value=em_collector):
                    with patch(
                        "greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone", return_value="unknown"
                    ):
                        await start_module.collect_carbon_intensity_for_all_zones()

    repository.save_history.assert_not_awaited()
    # No mappable zone: only DEFAULT_ZONE could be fetched, and the 24 h recompute still runs (NRG-03).
    combined_repo.recompute_carbon_with_latest_intensities.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_keeps_going_when_zone_fails():
    repository = MagicMock()
    repository.save_history = AsyncMock(side_effect=RuntimeError("insert failed"))
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(
        return_value={"node-a": NodeInfo(name="node-a", zone="eu-west-3a", region="eu-west-3", cloud_provider="aws")}
    )
    node_collector.close = AsyncMock()
    em_collector = MagicMock()
    em_collector.collect = AsyncMock(return_value=[{"datetime": "2026-04-30T12:00:00Z", "carbonIntensity": 50}])
    em_collector.close = AsyncMock()

    combined_repo = MagicMock()
    combined_repo.recompute_carbon_with_latest_intensities = AsyncMock(return_value=0)

    with patch("greenkube.cli.start.get_repository", return_value=repository):
        with patch("greenkube.cli.start.get_combined_metrics_repository", return_value=combined_repo):
            with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
                with patch("greenkube.cli.start.get_electricity_provider", return_value=em_collector):
                    with patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone", return_value="FR"):
                        await start_module.collect_carbon_intensity_for_all_zones()

    repository.save_history.assert_awaited_once()
    node_collector.close.assert_awaited_once()
    em_collector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_analyze_nodes_handles_initialization_failure():
    with patch("greenkube.cli.start.NodeCollector", side_effect=RuntimeError("k8s missing")):
        await start_module.analyze_nodes()


@pytest.mark.asyncio
async def test_analyze_nodes_ignores_node_metric_update_failure():
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(return_value={"node-a": NodeInfo(name="node-a", zone="eu-west-3a")})
    node_collector.close = AsyncMock()
    node_repo = MagicMock()
    node_repo.save_nodes = AsyncMock(return_value=1)

    with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
        with patch("greenkube.cli.start.get_node_repository", return_value=node_repo):
            with patch("greenkube.cli.start.update_node_metrics", side_effect=RuntimeError("gauge failed")):
                await start_module.analyze_nodes()

    node_repo.save_nodes.assert_awaited_once()
    node_collector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduled_write_metrics_delegates_to_async_writer():
    with patch("greenkube.cli.start.async_write_combined_metrics_to_database", new_callable=AsyncMock) as writer:
        await start_module.scheduled_write_metrics()

    writer.assert_awaited_once_with(last=None)


@pytest.mark.asyncio
async def test_async_write_combined_metrics_to_database_delegates_last_value():
    with patch("greenkube.cli.start.write_combined_metrics_to_database", new_callable=AsyncMock) as writer:
        await start_module.async_write_combined_metrics_to_database(last="1h")

    writer.assert_awaited_once_with(last="1h")


@pytest.mark.asyncio
async def test_async_write_combined_metrics_to_database_forwards_force():
    """Review 2: ``start --last 1h --force`` reaches the collection job."""
    with patch("greenkube.cli.start.write_combined_metrics_to_database", new_callable=AsyncMock) as writer:
        await start_module.async_write_combined_metrics_to_database(last="1h", force=True)

    writer.assert_awaited_once_with(last="1h", force=True)


@pytest.mark.asyncio
async def test_force_without_last_is_refused_by_the_collection_entry_point():
    from greenkube.cli import utils

    assert await utils.write_combined_metrics_to_database(last=None, force=True) is None


def test_data_processor_is_resolved_from_factory():
    with patch("greenkube.cli.start.get_data_processor") as get_processor:
        get_processor.return_value = MagicMock()
        assert start_module.get_data_processor() is get_processor.return_value
        get_processor.assert_called_once_with()


@pytest.mark.asyncio
async def test_run_recommendation_scan_ticks_the_scan_service_with_the_lease_gate():
    service = MagicMock()
    service.tick = AsyncMock(return_value=[object()])

    def should_run():
        return True

    with patch(
        "greenkube.core.optimization.scan_service.create_recommendation_scan_service", return_value=service
    ) as create:
        await start_module.run_recommendation_scan(should_continue=should_run)

    create.assert_called_once_with(should_continue=should_run)
    service.tick.assert_awaited_once_with(force=False)


@pytest.mark.asyncio
async def test_run_recommendation_scan_forwards_the_startup_force_flag():
    service = MagicMock()
    service.tick = AsyncMock(return_value=[])

    with patch("greenkube.core.optimization.scan_service.create_recommendation_scan_service", return_value=service):
        await start_module.run_recommendation_scan(force=True)

    service.tick.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_task", [False, True], ids=["completed", "failed-task"])
async def test_deferred_initial_work_always_reports_that_it_finished(failing_task):
    finished = []
    first = AsyncMock(side_effect=RuntimeError("boom") if failing_task else None)
    second = AsyncMock()

    await start_module._run_deferred_initial_controller_work(
        ((first, (), {}), (second, (), {})),
        lambda: True,
        asyncio.Event(),
        on_finished=lambda: finished.append(True),
    )

    assert finished == [True]
    assert second.await_count == (0 if failing_task else 1)


@pytest.mark.asyncio
async def test_deferred_initial_work_reports_finished_on_shutdown():
    finished = []
    stop_event = asyncio.Event()
    stop_event.set()
    task = AsyncMock()

    await start_module._run_deferred_initial_controller_work(
        ((task, (), {}),), lambda: False, stop_event, on_finished=lambda: finished.append(True)
    )

    task.assert_not_awaited()
    assert finished == [True]


@pytest.mark.asyncio
async def test_run_recommendation_scan_swallows_failures():
    with patch(
        "greenkube.core.optimization.scan_service.create_recommendation_scan_service",
        side_effect=RuntimeError("database unavailable"),
    ):
        await start_module.run_recommendation_scan()


@pytest.mark.asyncio
async def test_attribute_recommendation_savings_success():
    from greenkube.core.savings_attributor import AccrualResult

    reco_repo = MagicMock()
    reco_repo.get_applied_recommendations = AsyncMock(return_value=["rec"])
    savings_repo = MagicMock()
    attributor = MagicMock()
    attributor.accrue = AsyncMock(return_value=AccrualResult(slots=1, written=1))

    with patch("greenkube.core.config.get_config", return_value=SimpleNamespace(CLUSTER_NAME="cluster-a")):
        with patch("greenkube.core.factory.get_recommendation_repository", return_value=reco_repo):
            with patch("greenkube.core.factory.get_savings_ledger_repository", return_value=savings_repo):
                with (
                    patch("greenkube.core.savings_attributor.SavingsAttributor", return_value=attributor) as cls,
                    patch("greenkube.verify.ledger.projector.build_projector", return_value="projector"),
                ):
                    await start_module.attribute_recommendation_savings()

    # Dual write (C6): the attributor gets the savings ledger v2 projector.
    cls.assert_called_once_with(savings_repo=savings_repo, cluster_name="cluster-a", ledger="projector")
    attributor.accrue.assert_awaited_once_with(["rec"])


@pytest.mark.asyncio
async def test_attribute_recommendation_savings_handles_error():
    with patch("greenkube.core.factory.get_recommendation_repository", side_effect=RuntimeError("repo missing")):
        await start_module.attribute_recommendation_savings()


@pytest.mark.asyncio
async def test_compress_metrics_never_touches_the_savings_ledger():
    """C0.6 / BUG-32: ledger rows are append-only; the compression job leaves them alone."""
    compressor = MagicMock()
    compressor.run = AsyncMock(return_value={"hours_compressed": 2, "raw_rows_pruned": 3, "hourly_rows_pruned": 4})
    compressor.refresh_namespace_cache = AsyncMock()
    savings_repo = MagicMock()

    with patch("greenkube.core.metrics_compressor.MetricsCompressor", return_value=compressor):
        with patch("greenkube.core.db.get_db_manager", return_value=MagicMock()):
            with patch("greenkube.core.factory.get_savings_ledger_repository", return_value=savings_repo):
                await start_module.compress_metrics()

    compressor.run.assert_awaited_once()
    compressor.refresh_namespace_cache.assert_awaited_once()
    assert savings_repo.method_calls == []


@pytest.mark.asyncio
async def test_compress_metrics_handles_errors():
    with patch("greenkube.core.metrics_compressor.MetricsCompressor", side_effect=RuntimeError("compressor failed")):
        await start_module.compress_metrics()


@pytest.mark.asyncio
async def test_refresh_dashboard_summary_success():
    summary_repo = MagicMock()
    summary_repo.get_rows = AsyncMock(return_value=["row"])
    refresher = MagicMock()
    refresher.run = AsyncMock(return_value=1)
    update_metrics = MagicMock()

    with patch("greenkube.core.factory.get_summary_repository", return_value=summary_repo):
        with patch("greenkube.core.factory.get_combined_metrics_repository", return_value=MagicMock()):
            with patch("greenkube.core.factory.get_timeseries_cache_repository", return_value=MagicMock()):
                with patch("greenkube.core.summary_refresher.SummaryRefresher", return_value=refresher):
                    with patch("greenkube.api.metrics_endpoint.update_dashboard_summary_metrics", update_metrics):
                        await start_module.refresh_dashboard_summary()

    refresher.run.assert_awaited_once()
    summary_repo.get_rows.assert_awaited_once_with(namespace=None)
    update_metrics.assert_called_once_with(["row"], reset=True)


@pytest.mark.asyncio
async def test_refresh_dashboard_summary_handles_errors():
    with patch("greenkube.core.factory.get_summary_repository", side_effect=RuntimeError("repo missing")):
        await start_module.refresh_dashboard_summary()


class _CapturingRegistry:
    """Stands in for LoopRegistry: records the declared loops without running them."""

    instances: list = []

    def __init__(self, *, role="controller", leader=None):
        self.role = role
        self.leader = leader
        self.specs = {}
        self.started = False
        self.stop = AsyncMock()
        _CapturingRegistry.instances.append(self)

    def set_leader(self, leader):
        self.leader = leader

    def register(self, spec):
        assert spec.name not in self.specs
        self.specs[spec.name] = spec
        return spec

    @property
    def names(self):
        return tuple(self.specs)

    def start(self):
        self.started = True

    def spec(self, name):
        return self.specs[name]

    async def run_once(self, name):
        await self.specs[name].func()
        return "success"


def _controller_config(**overrides):
    values = dict(
        LOG_LEVEL="INFO",
        LOG_FORMAT="console",
        CLUSTER_NAME="test-cluster",
        DB_TYPE="sqlite",
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        NODE_ANALYSIS_INTERVAL="1h",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_async_start_registers_every_controller_loop_and_runs_initial_tasks(monkeypatch):
    """M8: every periodic job is declared once in the loop registry, with its former cadence."""
    _CapturingRegistry.instances.clear()
    db_manager = MagicMock()
    db_manager.connect = AsyncMock()
    stop_event = MagicMock()
    stop_event.wait = AsyncMock(return_value=None)
    loop = MagicMock()
    monkeypatch.setattr("greenkube.automation.plan_runtime.initialize_gitops_provider_configs", AsyncMock())
    monkeypatch.setattr(
        "greenkube.gitops.discovery.runtime.create_gitops_discovery_runtime",
        AsyncMock(return_value=SimpleNamespace(run_once=AsyncMock(), interval_seconds=300.0)),
    )
    monkeypatch.setattr("greenkube.core.loops.LoopRegistry", _CapturingRegistry)
    monkeypatch.setattr(start_module, "_create_controller_plan_runtime", AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, "_create_session_purge", lambda _db: AsyncMock())

    with patch("greenkube.cli.start.get_config", return_value=_controller_config()):
        with patch("greenkube.core.db.get_db_manager", return_value=db_manager):
            with (
                patch("greenkube.cli.start.collect_carbon_intensity_for_all_zones", new_callable=AsyncMock) as carbon,
                patch("greenkube.cli.start.analyze_nodes", new_callable=AsyncMock) as analyze,
                patch(
                    "greenkube.cli.start.async_write_combined_metrics_to_database", new_callable=AsyncMock
                ) as write_metrics,
                patch("greenkube.cli.start.attribute_recommendation_savings", new_callable=AsyncMock) as attribute,
                patch("greenkube.cli.start.compress_metrics", new_callable=AsyncMock) as compress,
                patch("greenkube.cli.start.refresh_dashboard_summary", new_callable=AsyncMock) as refresh,
                patch("greenkube.cli.start.run_recommendation_lifecycle", new_callable=AsyncMock),
                patch("greenkube.cli.start.asyncio.Event", return_value=stop_event),
                patch("greenkube.cli.start.asyncio.get_running_loop", return_value=loop),
                patch("greenkube.cli.start.run_recommendation_scan", new_callable=AsyncMock) as scan,
            ):
                await start_module._async_start(last="1h")

    registry = _CapturingRegistry.instances[-1]
    assert registry.started
    assert registry.names == (
        "collection",
        "carbon-intensity",
        "node-analysis",
        "savings-attribution",
        "recommendation-lifecycle",
        "savings-drift",
        "ledger-chain",
        "recommendation-scan",
        "metrics-compression",
        "dashboard-summary",
        "gitops-discovery",
        "session-purge",
        "controller-heartbeat",
    )
    specs = registry.specs
    # Every replica publishes its loop states; every other job runs on the leader only.
    assert [name for name, spec in specs.items() if not spec.leader_only] == ["controller-heartbeat"]
    # Collection fires on step boundaries plus the safety lag; scans poll every 15s.
    assert specs["collection"].interval_seconds == 300
    assert specs["collection"].align_offset_seconds == 60.0
    assert specs["recommendation-scan"].interval_seconds == 15
    assert specs["node-analysis"].interval_seconds == 3600
    # Jobs awaited once by the initial work do not run again at registry start.
    for name in ("carbon-intensity", "metrics-compression", "dashboard-summary", "recommendation-scan"):
        assert not specs[name].run_immediately
    assert specs["gitops-discovery"].run_immediately
    assert specs["gitops-discovery"].standby_recheck_seconds == 2.0
    scan.assert_awaited_once_with(should_continue=None, force=True)
    carbon.assert_awaited_once()
    analyze.assert_awaited_once()
    write_metrics.assert_awaited_once_with(last="1h")
    attribute.assert_awaited_once()
    compress.assert_awaited_once()
    refresh.assert_awaited_once()
    stop_event.wait.assert_awaited_once()
    registry.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_start_registers_automation_loops_only_with_a_plan_runtime(monkeypatch):
    """F8: sync, the automation controller loop and plan execution run in the controller."""
    _CapturingRegistry.instances.clear()
    db_manager = MagicMock()
    db_manager.connect = AsyncMock()
    stop_event = MagicMock()
    stop_event.wait = AsyncMock(return_value=None)
    worker = SimpleNamespace(
        pull_request_repository=object(),
        plans=object(),
        operations=object(),
        provider_resolver=SimpleNamespace(provider_repository=object(), registry=object()),
    )
    plan_runtime = SimpleNamespace(worker=worker, plan_service=object(), gitops_repository=object())
    sync_runtime = SimpleNamespace(run_once=AsyncMock(), interval_seconds=30.0)
    monkeypatch.setattr("greenkube.automation.plan_runtime.initialize_gitops_provider_configs", AsyncMock())
    monkeypatch.setattr(
        "greenkube.gitops.discovery.runtime.create_gitops_discovery_runtime",
        AsyncMock(return_value=SimpleNamespace(run_once=AsyncMock(), interval_seconds=300.0)),
    )
    monkeypatch.setattr("greenkube.core.loops.LoopRegistry", _CapturingRegistry)
    monkeypatch.setattr(start_module, "_create_controller_plan_runtime", AsyncMock(return_value=plan_runtime))
    monkeypatch.setattr(start_module, "_create_gitops_sync_runtime", lambda runtime, db: sync_runtime)
    monkeypatch.setattr(start_module, "_create_session_purge", lambda _db: None)
    monkeypatch.setenv("GITOPS_PR_POLL_INTERVAL", "120")
    for name in (
        "collect_carbon_intensity_for_all_zones",
        "analyze_nodes",
        "async_write_combined_metrics_to_database",
        "attribute_recommendation_savings",
        "run_recommendation_lifecycle",
        "compress_metrics",
        "refresh_dashboard_summary",
        "run_recommendation_scan",
    ):
        monkeypatch.setattr(start_module, name, AsyncMock())

    config = _controller_config()
    with patch("greenkube.cli.start.get_config", return_value=config):
        with patch("greenkube.core.db.get_db_manager", return_value=db_manager):
            with (
                patch("greenkube.cli.start.asyncio.Event", return_value=stop_event),
                patch("greenkube.cli.start.asyncio.get_running_loop", return_value=MagicMock()),
            ):
                await start_module._async_start(last=None)
    registry = _CapturingRegistry.instances[-1]
    assert "gitops-sync" in registry.names
    assert "automation-controller" in registry.names
    # F12: always on. The executor and the readiness reconciler are always registered;
    # the reconciler switches delivery and PR tracking at run time.
    assert "plan-executor" in registry.names
    assert "automation-readiness" in registry.names
    readiness = registry.specs["automation-readiness"].func.__self__
    executor = registry.specs["plan-executor"].func.__self__
    controller = registry.specs["automation-controller"].func.__self__
    executor_spec = registry.specs["plan-executor"]
    # A standby controller stops reporting itself as an up executor.
    assert executor_spec.on_standby == executor_spec.func.__self__.standby
    assert readiness.delivery_active is False
    assert executor.active() is False
    assert controller.delivery_enabled is False
    readiness._snapshot = SimpleNamespace(
        capabilities={
            "delivery": SimpleNamespace(active=True),
            "sync_tracking": SimpleNamespace(active=True),
        }
    )
    assert executor.active() is True
    assert controller.delivery_enabled is True
    assert registry.specs["automation-controller"].leader_only
    assert registry.specs["automation-readiness"].leader_only
    assert registry.specs["gitops-sync"].func is sync_runtime.run_once


@pytest.mark.asyncio
async def test_async_start_runs_and_cancels_gitops_discovery_after_provider_seeding(monkeypatch):
    order: list[str] = []
    discovery_started = asyncio.Event()
    discovery_stopped = asyncio.Event()
    db_manager = MagicMock()

    async def connect():
        order.append("database")

    db_manager.connect = AsyncMock(side_effect=connect)
    processor = MagicMock()
    processor.close = AsyncMock()

    class _Runtime:
        interval_seconds = 300.0

        async def run_once(self):
            task = asyncio.current_task()
            order.append("discovery")
            discovery_started.set()
            try:
                await asyncio.Future()
            finally:
                assert task is not None
                order.append(task.get_name())
                discovery_stopped.set()

    class _StopEvent:
        def set(self):
            pass

        async def wait(self):
            await discovery_started.wait()

    async def seed_provider_configs(*, db_manager):
        assert db_manager is not None
        order.append("provider-seed")

    async def build_discovery_runtime(*, db_manager):
        assert db_manager is not None
        order.append("runtime-build")
        return _Runtime()

    monkeypatch.setattr(start_module, "_create_controller_plan_runtime", AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, "_create_session_purge", lambda _db: None)
    for name in (
        "collect_carbon_intensity_for_all_zones",
        "analyze_nodes",
        "async_write_combined_metrics_to_database",
        "attribute_recommendation_savings",
        "run_recommendation_lifecycle",
        "compress_metrics",
        "refresh_dashboard_summary",
        "run_recommendation_scan",
    ):
        monkeypatch.setattr(start_module, name, AsyncMock())
    loop = MagicMock()
    with patch("greenkube.cli.start.get_config", return_value=_controller_config()):
        with patch("greenkube.core.db.get_db_manager", return_value=db_manager):
            with (
                patch("greenkube.automation.plan_runtime.initialize_gitops_provider_configs", seed_provider_configs),
                patch("greenkube.gitops.discovery.runtime.create_gitops_discovery_runtime", build_discovery_runtime),
                patch("greenkube.cli.start.get_data_processor", return_value=processor),
                patch("greenkube.cli.start.asyncio.Event", return_value=_StopEvent()),
                patch("greenkube.cli.start.asyncio.get_running_loop", return_value=loop),
            ):
                await start_module._async_start(last=None)

    assert order[:3] == ["database", "provider-seed", "runtime-build"]
    assert "discovery" in order
    assert "loop-gitops-discovery" in order
    assert discovery_stopped.is_set()
    processor.close.assert_awaited_once()


def test_start_returns_when_subcommand_invoked():
    ctx = MagicMock()
    ctx.invoked_subcommand = "child"

    with patch("greenkube.cli.start.asyncio.run") as run:
        start_module.start(ctx)

    run.assert_not_called()


def test_start_ignores_keyboard_interrupt():
    ctx = MagicMock()
    ctx.invoked_subcommand = None

    with patch("greenkube.cli.start._async_start", return_value="startup-coroutine"):
        with patch("greenkube.cli.start.asyncio.run", side_effect=KeyboardInterrupt):
            start_module.start(ctx)


def test_start_raises_typer_exit_on_unexpected_error():
    ctx = MagicMock()
    ctx.invoked_subcommand = None

    with patch("greenkube.cli.start._async_start", return_value="startup-coroutine"):
        with patch("greenkube.cli.start.asyncio.run", side_effect=RuntimeError("boom")):
            with pytest.raises(typer.Exit) as exc_info:
                start_module.start(ctx)

    assert exc_info.value.exit_code == 1


# -- Upgrade safety: the controller answers its probes while it migrates --------------
# Migrations run at controller startup. The startup probe targets /healthz on the
# controller metrics listener; when that listener only started after the migrations, a
# migration longer than the probe budget (10 minutes by default) got the pod restarted,
# the migration transaction rolled back, and the upgrade crash-looped.


class _ReadyRegistry(_CapturingRegistry):
    def ready(self):
        return True

    def snapshot(self):
        return {"role": self.role, "ready": True, "loops": []}


@pytest.mark.asyncio
async def test_metrics_listener_serves_before_migrations_and_reports_not_ready(monkeypatch):
    events: list[str] = []
    captured: dict = {}

    async def fake_start_metrics(port, *args, health=None, **kwargs):
        events.append("metrics-started")
        captured["health"] = health
        return "endpoint"

    async def fake_stop_metrics(handle):
        events.append(f"metrics-stopped:{handle}")

    async def connect():
        events.append("migrations")
        # While migrating, the probe listener is up but the controller is not ready.
        assert captured["health"].ready() is False
        assert captured["health"].snapshot()["ready"] is False

    db_manager = MagicMock()
    db_manager.connect = AsyncMock(side_effect=connect)
    stop_event = MagicMock()

    async def wait():
        # Loops have started by the time the controller waits for a signal.
        assert captured["health"].ready() is True
        events.append("running")

    stop_event.wait = AsyncMock(side_effect=wait)
    monkeypatch.setattr("greenkube.automation.plan_runtime.initialize_gitops_provider_configs", AsyncMock())
    monkeypatch.setattr(
        "greenkube.gitops.discovery.runtime.create_gitops_discovery_runtime",
        AsyncMock(return_value=SimpleNamespace(run_once=AsyncMock(), interval_seconds=300.0)),
    )
    monkeypatch.setattr("greenkube.core.loops.LoopRegistry", _ReadyRegistry)
    monkeypatch.setattr("greenkube.cli.controller_metrics.start_controller_metrics_server", fake_start_metrics)
    monkeypatch.setattr("greenkube.cli.controller_metrics.stop_controller_metrics_server", fake_stop_metrics)
    monkeypatch.setattr(start_module, "_create_controller_plan_runtime", AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, "_create_session_purge", lambda _db: AsyncMock())

    initial_tasks = (
        "collect_carbon_intensity_for_all_zones",
        "analyze_nodes",
        "async_write_combined_metrics_to_database",
        "attribute_recommendation_savings",
        "compress_metrics",
        "refresh_dashboard_summary",
        "run_recommendation_lifecycle",
        "run_recommendation_scan",
    )
    for name in initial_tasks:
        monkeypatch.setattr(start_module, name, AsyncMock())

    with (
        patch("greenkube.cli.start.get_config", return_value=_controller_config(CONTROLLER_METRICS_PORT=9091)),
        patch("greenkube.core.db.get_db_manager", return_value=db_manager),
        patch("greenkube.cli.start.asyncio.Event", return_value=stop_event),
        patch("greenkube.cli.start.asyncio.get_running_loop", return_value=MagicMock()),
    ):
        await start_module._async_start(last=None)

    assert events == ["metrics-started", "migrations", "running", "metrics-stopped:endpoint"]


@pytest.mark.asyncio
async def test_failed_migration_stops_the_metrics_listener(monkeypatch):
    events: list[str] = []

    async def fake_start_metrics(port, *args, health=None, **kwargs):
        events.append("metrics-started")
        return "endpoint"

    async def fake_stop_metrics(handle):
        events.append(f"metrics-stopped:{handle}")

    db_manager = MagicMock()
    db_manager.connect = AsyncMock(side_effect=RuntimeError("migration failed"))
    monkeypatch.setattr("greenkube.core.loops.LoopRegistry", _ReadyRegistry)
    monkeypatch.setattr("greenkube.cli.controller_metrics.start_controller_metrics_server", fake_start_metrics)
    monkeypatch.setattr("greenkube.cli.controller_metrics.stop_controller_metrics_server", fake_stop_metrics)

    with (
        patch("greenkube.cli.start.get_config", return_value=_controller_config(CONTROLLER_METRICS_PORT=9091)),
        patch("greenkube.core.db.get_db_manager", return_value=db_manager),
    ):
        with pytest.raises(RuntimeError, match="migration failed"):
            await start_module._async_start(last=None)

    assert events == ["metrics-started", "metrics-stopped:endpoint"]


@pytest.mark.asyncio
async def test_initial_pass_of_a_registered_loop_counts_as_a_run():
    """F-10: the initial metrics-compression pass shows in the loop status (``runs: 1``)."""
    from greenkube.core.loops import LoopRegistry, LoopSpec

    registry = LoopRegistry(role="controller")
    compress = AsyncMock()
    collect = AsyncMock()
    registry.register(LoopSpec("metrics-compression", compress, 3600))

    initial = start_module._initial_tasks_as_loop_ticks(registry, ((collect, (), {"last": "1h"}), (compress, (), {})))
    for callback, args, kwargs in initial:
        assert await start_module._run_initial_controller_task(callback, None, *args, **kwargs)

    compress.assert_awaited_once()
    collect.assert_awaited_once_with(last="1h")
    status = registry.status("metrics-compression")
    assert status.runs == 1
    assert status.last_outcome == "success"
    assert status.last_success_at is not None


@pytest.mark.asyncio
async def test_initial_tick_skipped_after_lease_loss_does_not_count_as_done(caplog):
    """A startup tick skipped because the Lease was lost after ``should_run()`` is not complete work."""
    from greenkube.core.loops import LoopRegistry, LoopSpec

    registry = LoopRegistry(role="controller", leader=lambda: False)
    compress = AsyncMock()
    registry.register(LoopSpec("metrics-compression", compress, 3600))
    ((callback, args, kwargs),) = start_module._initial_tasks_as_loop_ticks(registry, ((compress, (), {}),))

    with caplog.at_level("INFO", logger="greenkube.cli.start"):
        done = await start_module._run_initial_controller_task(callback, lambda: True, *args, **kwargs)

    assert done is False
    compress.assert_not_awaited()
    assert any("metrics-compression" in record.getMessage() for record in caplog.records)
