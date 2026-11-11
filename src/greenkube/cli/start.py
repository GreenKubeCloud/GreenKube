# src/greenkube/cli/start.py
"""
Start command for the GreenKube CLI.

This module contains the scheduler start command and the helper that
collects carbon intensity for all zones. It mirrors the previous logic
that lived in `cli.main` but is isolated for clarity and easier testing.
"""

import asyncio
import logging
import signal
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Optional, Set

import typer
from typing_extensions import Annotated

from ..collectors.node_collector import NodeCollector
from ..core.config import get_config
from ..core.factory import (
    get_combined_metrics_repository,
    get_data_processor,
    get_electricity_provider,
    get_node_repository,
    get_repository,
)
from ..core.loops import report_loop_failure
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone
from .utils import write_combined_metrics_to_database

try:
    from ..api.metrics_endpoint import update_node_metrics
except Exception:  # pragma: no cover – optional dependency
    update_node_metrics = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

app = typer.Typer(name="start", help="Start the GreenKube data collection service.")


async def collect_carbon_intensity_for_all_zones() -> None:
    """
    Orchestrates the collection and saving of carbon intensity data.
    """
    logger.info("--- Starting hourly carbon intensity collection task ---")
    try:
        repository = get_repository()
        node_collector = NodeCollector()
        em_collector = get_electricity_provider()
    except Exception as e:
        logger.error("Failed to initialize components for intensity collection: %s", e)
        report_loop_failure(e)
        return

    try:
        nodes_info = await node_collector.collect()
        if not nodes_info:
            logger.warning("No node zones discovered.")
            return

        # Collect unique (zone, region, provider) tuples for mapping.
        unique_zone_region_providers = {
            (node_info.zone, node_info.region, node_info.cloud_provider)
            for node_info in nodes_info.values()
            if node_info.zone or node_info.region
        }
        emaps_zones: Set[str] = set()
        for cz, region, provider in unique_zone_region_providers:
            emz = None
            # Try zone first
            if cz:
                emz = get_emaps_zone_from_cloud_zone(cz, provider=provider)
            # Fallback to region (mirrors NodeZoneMapper logic)
            if not emz and region:
                emz = get_emaps_zone_from_cloud_zone(region, provider=provider)
            if emz and emz != "unknown":
                emaps_zones.add(emz)
            else:
                logger.warning(
                    "Could not map cloud zone '%s' or region '%s' (provider: %s) to an Electricity Maps zone.",
                    cz,
                    region,
                    provider,
                )

        if not emaps_zones:
            logger.warning("No mappable Electricity Maps zones found based on node discovery.")
            return

        # Parallelize zone history collection
        async def process_zone(zone):
            import structlog as _structlog

            _structlog.contextvars.bind_contextvars(collector=get_config().ELECTRICITY_PROVIDER, zone=zone)
            try:
                history_data = await em_collector.collect(zone=zone)
                if history_data:
                    saved_count = await repository.save_history(history_data, zone=zone)
                    logger.info("Successfully saved %s new records for zone: %s", saved_count, zone)
                else:
                    logger.info("No new data to save for zone: %s", zone)
            except Exception as e:
                logger.error("Failed to process data for zone %s: %s", zone, e)
                report_loop_failure(e)

        await asyncio.gather(*(process_zone(zone) for zone in emaps_zones))

        await _recompute_combined_metrics(repository)

    except Exception as e:
        logger.error("Failed to collect node zones: %s", e)
        report_loop_failure(e)
    finally:
        await node_collector.close()
        await em_collector.close()

    logger.info("--- Finished carbon intensity collection task ---")


# Window (hours) for which combined metrics are recomputed after each
# intensity refresh. Must cover the upstream consolidation horizon (Wattnet
# data is final ~4h after measurement) with margin; matches the 24h history
# window fetched by the providers.
CARBON_RECOMPUTE_WINDOW_HOURS = 24


async def _recompute_combined_metrics(carbon_intensity_repository) -> None:
    """
    Recompute combined metrics against the refreshed carbon intensities.

    Providers such as Wattnet publish provisional intensity values that are
    consolidated (made final) a few hours later. This recomputes **all**
    combined metrics in the last ``CARBON_RECOMPUTE_WINDOW_HOURS`` — not only
    those without an intensity — so rows calculated from provisional data are
    corrected in place.
    """
    try:
        combined_repo = get_combined_metrics_repository()
        now = datetime.now(timezone.utc)
        updated = await combined_repo.recompute_carbon_with_latest_intensities(
            carbon_intensity_repository,
            now - timedelta(hours=CARBON_RECOMPUTE_WINDOW_HOURS),
            now,
        )
        logger.info("Recomputed %d combined metrics with consolidated intensities.", updated)
    except Exception as e:
        logger.error("Failed to recompute combined metrics with latest intensities: %s", e)
        report_loop_failure(e)


async def analyze_nodes() -> None:
    """
    Collects node information and updates the database.
    """
    logger.info("--- Starting node analysis task ---")
    try:
        node_collector = NodeCollector()
        node_repo = get_node_repository()
    except Exception as e:
        logger.error("Failed to initialize components for node analysis: %s", e)
        report_loop_failure(e)
        return

    try:
        nodes_info = await node_collector.collect()
        if not nodes_info:
            logger.warning("No nodes discovered during analysis.")
            return

        saved_count = await node_repo.save_nodes(list(nodes_info.values()))
        logger.info("Successfully updated %s nodes in the database.", saved_count)

        # Update Prometheus gauges for Grafana scraping
        if update_node_metrics is not None:
            try:
                update_node_metrics(list(nodes_info.values()))
            except Exception as e:
                logger.warning("Failed to update Prometheus node metrics: %s", e)

    except Exception as e:
        logger.error("Failed to analyze nodes: %s", e)
        report_loop_failure(e)
    finally:
        if "node_collector" in locals():
            await node_collector.close()

    logger.info("--- Finished node analysis task ---")


async def async_write_combined_metrics_to_database(last: Optional[str] = None):
    """Wrapper to make write_combined_metrics_to_database suitable for scheduler."""
    from ..core.observability import elapsed, record_collection_run, start_timer

    timer = start_timer()
    try:
        await write_combined_metrics_to_database(last=last)
    finally:
        record_collection_run(elapsed(timer))


async def scheduled_write_metrics():
    """Scheduled task wrapper for writing combined metrics (always uses last=None)."""
    await async_write_combined_metrics_to_database(last=None)


async def attribute_recommendation_savings() -> None:
    """Accrue savings of applied recommendations on the fixed UTC ledger grid.

    Every complete hour slot since the stored watermark is accrued exactly once,
    so restarts and downtime neither double count nor lose savings.
    """
    logger.info("--- Starting savings attribution task ---")
    try:
        from ..core.factory import get_recommendation_repository as _reco_repo
        from ..core.factory import get_savings_ledger_repository as _savings_repo
        from ..core.savings_attributor import SavingsAttributor, savings_cluster_name

        applied = await _reco_repo().get_applied_recommendations()
        attributor = SavingsAttributor(savings_repo=_savings_repo(), cluster_name=savings_cluster_name())
        result = await attributor.accrue(applied)
        logger.info(
            "Savings attribution complete: %d slot(s), %d row(s) written, %d error(s).",
            result.slots,
            result.written,
            result.errors,
        )
    except Exception as e:
        logger.error("Savings attribution task failed: %s", e)
        report_loop_failure(e)
    logger.info("--- Finished savings attribution task ---")


async def compress_metrics() -> None:
    """Compress old raw metrics into hourly aggregates and prune stale data.

    The savings ledger is append-only and is never compressed or pruned here:
    its hourly rollups are read-side views (roadmap C0.6).
    """
    logger.info("--- Starting metrics compression task ---")
    try:
        from ..core.db import get_db_manager
        from ..core.metrics_compressor import MetricsCompressor

        compressor = MetricsCompressor(get_db_manager())
        stats = await compressor.run()
        # Also refresh the namespace cache
        await compressor.refresh_namespace_cache()
        logger.info(
            "Compression stats: %d hours compressed, %d raw pruned, %d hourly pruned",
            stats["hours_compressed"],
            stats["raw_rows_pruned"],
            stats["hourly_rows_pruned"],
        )
    except Exception as e:
        logger.error("Metrics compression failed: %s", e)
        report_loop_failure(e)
    logger.info("--- Finished metrics compression task ---")


async def refresh_dashboard_summary() -> None:
    """Refresh the pre-computed dashboard summary table."""
    logger.info("--- Starting dashboard summary refresh task ---")
    try:
        from ..api.metrics_endpoint import update_dashboard_summary_metrics
        from ..core.factory import (
            get_combined_metrics_repository,
            get_summary_repository,
            get_timeseries_cache_repository,
        )
        from ..core.summary_refresher import SummaryRefresher

        summary_repo = get_summary_repository()
        refresher = SummaryRefresher(
            metrics_repo=get_combined_metrics_repository(),
            summary_repo=summary_repo,
            timeseries_cache_repo=get_timeseries_cache_repository(),
        )
        count = await refresher.run()
        rows = await summary_repo.get_rows(namespace=None)
        update_dashboard_summary_metrics(rows, reset=True)
        logger.info("Dashboard summary refresh complete: %d rows upserted.", count)
    except Exception as e:
        logger.error("Dashboard summary refresh failed: %s", e)
        report_loop_failure(e)
    logger.info("--- Finished dashboard summary refresh task ---")


async def run_recommendation_lifecycle() -> None:
    """Run apply detection, outcome verification and TTL expiry (Phase 3).

    This is the scheduled counterpart of the PR bot: once a change lands in the
    cluster (manually, via PR merge or via ArgoCD sync), the detector flips the
    recommendation to ``applied`` and the verifier compares the measured cost,
    carbon and workload-health signals against the frozen baseline.
    """
    logger.info("--- Starting recommendation lifecycle task ---")
    try:
        from ..core.factory import (
            get_combined_metrics_repository as _combined_repo,
        )
        from ..core.factory import get_gitops_repository as _gitops_repo
        from ..core.factory import (
            get_recommendation_repository as _reco_repo,
        )
        from ..core.optimization.applied_detector import AppliedDetector
        from ..core.optimization.lifecycle import RecommendationLifecycle
        from ..core.optimization.verifier import RecommendationVerifier

        repo = _reco_repo()
        lifecycle = RecommendationLifecycle(repo)

        expired = await lifecycle.expire()
        if expired:
            logger.info("Expired %d recommendation(s) past their TTL.", len(expired))

        from ..collectors.workload_health_collector import WorkloadHealthCollector
        from ..core.factory import get_pull_request_repository as _pr_repo
        from ..storage.change_repository import get_change_repository
        from ..storage.measurement_repository import get_savings_measurement_repository
        from ..verify.load_driver import PrometheusRangeReader
        from ..verify.service import ChangeMeasurementService

        health_collector = WorkloadHealthCollector()
        load_reader = PrometheusRangeReader()
        combined_repo = _combined_repo()
        verifier = RecommendationVerifier(
            lifecycle,
            combined_repo,
            health_collector=health_collector,
            measurement_repo=get_savings_measurement_repository(),
        )
        # Change measurement (C1-C5): the detector records changes, run_due measures them.
        changes = ChangeMeasurementService(
            lifecycle,
            combined_repo,
            get_change_repository(),
            verifier=verifier,
            load_reader=load_reader,
            pull_request_repo=_pr_repo(),
        )
        try:
            detector = AppliedDetector(lifecycle, action_status_repository=_gitops_repo(), change_recorder=changes)
            applied = await detector.detect()
            if applied:
                logger.info("Detected %d newly applied recommendation(s).", len(applied))
            verified = await verifier.verify_due()
            verified += await changes.run_due()
        finally:
            await health_collector.close()
            await load_reader.close()
        if verified:
            logger.info("Verified %d recommendation outcome(s).", len(verified))

        # Stop attributing savings for outcomes that need rollback review.
        rollbacks = [
            r for r in verified if r.id is not None and getattr(r.status, "value", r.status) == "rollback_review"
        ]
        if rollbacks:
            from ..core.factory import get_savings_ledger_repository as _savings_repo

            savings_repo = _savings_repo()
            for record in rollbacks:
                rec_id = record.id
                if rec_id is None:
                    continue
                try:
                    await savings_repo.supersede_for_recommendation(rec_id)
                except Exception as exc:
                    logger.warning("Could not supersede savings for recommendation %s: %s", rec_id, exc)
    except Exception as e:
        logger.error("Recommendation lifecycle task failed: %s", e)
        report_loop_failure(e)
    logger.info("--- Finished recommendation lifecycle task ---")


def _collection_offset_seconds(cfg) -> float:
    from ..collectors.promql import parse_duration_seconds

    try:
        return float(parse_duration_seconds(getattr(cfg, "COLLECTION_SAFETY_LAG", "1m")))
    except ValueError:
        return 60.0


async def run_recommendation_scan(
    should_continue: Callable[[], bool] | None = None,
    *,
    force: bool = False,
) -> None:
    """Schedule and execute queued recommendation scans (E1).

    Runs only in the controller, behind the Lease: each cycle queues the
    scheduled scan when ``RECOMMENDATION_SCAN_INTERVAL`` elapsed and executes
    queued runs, including API requests from ``POST /optimization/runs``.
    ``force`` queues a cluster-wide scan regardless of the interval; the
    controller uses it once, right after its initial collection.
    """
    try:
        from ..core.optimization.scan_service import create_recommendation_scan_service

        service = create_recommendation_scan_service(should_continue=should_continue)
        executed = await service.tick(force=force)
        if executed:
            logger.info("Recommendation scan cycle executed %d run(s).", len(executed))
    except Exception as e:
        logger.error("Recommendation scan task failed: %s", e)
        report_loop_failure(e)


class _InitialControllerWork:
    """Whether this controller finished its initial (post-start) work cycle.

    The recommendation scan poll stays idle until then: a poll firing while
    the initial collection is still writing metrics would scan an empty or
    stale dataset and cover the whole scan interval with it.
    """

    def __init__(self) -> None:
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def mark_done(self) -> None:
        self._done = True


def _create_controller_leader_election(cfg):
    """Build the optional controller Lease elector from the chart-provided settings."""
    if not getattr(cfg, "LEADER_ELECTION_ENABLED", False):
        return None

    from ..core.k8s_client import get_coordination_v1_api
    from ..core.leader_election import KubernetesLeaseElector, process_identity

    lease_name = getattr(cfg, "LEADER_ELECTION_LEASE_NAME", "")
    lease_namespace = getattr(cfg, "LEADER_ELECTION_LEASE_NAMESPACE", "")
    if not lease_name or not lease_namespace:
        raise ValueError("LEADER_ELECTION_LEASE_NAME and LEADER_ELECTION_LEASE_NAMESPACE are required when enabled.")

    return KubernetesLeaseElector(
        name=lease_name,
        namespace=lease_namespace,
        identity=process_identity(),
        api_provider=get_coordination_v1_api,
    )


def _leader_fence_scope(should_run: Callable[[], bool] | None):
    """Give the job a LeaderFence so it can re-check the Lease before committing (A14)."""
    from ..core.leader_election import LeaderGate, leader_fence_scope

    return leader_fence_scope(should_run.fence() if isinstance(should_run, LeaderGate) else None)


async def _run_initial_controller_task(
    callback: Callable[..., Awaitable[None]],
    should_run: Callable[[], bool] | None,
    *args,
    **kwargs,
) -> bool:
    if should_run is not None and not should_run():
        logger.info("Skipping remaining initial controller work while this process is not the Lease holder.")
        return False
    with _leader_fence_scope(should_run):
        await callback(*args, **kwargs)
    return True


async def _run_deferred_initial_controller_work(
    initial_tasks,
    should_run,
    stop_event: asyncio.Event,
    on_finished: Callable[[], None] | None = None,
) -> None:
    """Run each startup task once when this controller first acquires its Lease.

    ``on_finished`` is called however the work ends (complete, failed task,
    shutdown) so jobs waiting for the initial cycle never stay idle forever.
    """
    from ..core.leader_election import DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS

    try:
        for callback, args, kwargs in initial_tasks:
            while not stop_event.is_set():
                if should_run():
                    try:
                        if await _run_initial_controller_task(callback, should_run, *args, **kwargs):
                            break
                    except Exception:
                        logger.exception("Deferred initial controller task %s failed.", callback.__name__)
                        return
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS,
                    )
                except TimeoutError:
                    continue
            else:
                return
        logger.info("Initial controller work complete.")
    finally:
        if on_finished is not None:
            on_finished()


async def _create_controller_plan_runtime(db_manager):
    """Compose the GitOps plan runtime for the controller, or ``None`` when Git providers are absent."""
    from ..automation.plan_runtime import (
        PlanProvidersNotConfiguredError,
        PlanRuntimeConfigurationError,
        create_plan_runtime,
    )

    try:
        # Automation is always on (F12): the runtime is composed even without explicit
        # providers; the readiness reconciler infers them from discovered repositories.
        return await create_plan_runtime(db_manager=db_manager, connect_db=False, allow_empty=True)
    except PlanProvidersNotConfiguredError as error:
        logger.info("GitOps sync observation and automation loops are idle: %s", error)
    except PlanRuntimeConfigurationError as error:
        logger.warning("GitOps sync observation and automation loops are unavailable: %s", error)
    except Exception as error:
        logger.warning("GitOps sync observation and automation loops are unavailable (%s).", type(error).__name__)
    return None


def _create_gitops_sync_runtime(plan_runtime, db_manager):
    """Bounded sync observation with PR status comments (J5) over the plan runtime's repositories."""
    from ..automation.sync_runtime import create_gitops_sync_runtime

    try:
        from ..automation.pr_feedback import create_pull_request_feedback_publisher

        # One status comment per merged PR: synced, applied, measuring, outcome (J5).
        feedback = create_pull_request_feedback_publisher(
            pull_request_repository=plan_runtime.pull_requests,
            plan_repository=plan_runtime.gitops_repository,
            provider_resolver=plan_runtime.provider_resolver,
            recommendation_repository=plan_runtime.recommendations,
            db_manager=db_manager,
        )
    except Exception as error:
        logger.warning("PR status comments are unavailable (%s).", type(error).__name__)
        feedback = None
    return create_gitops_sync_runtime(
        plan_runtime.pull_requests,
        plan_runtime.gitops_repository,
        feedback=feedback,
    )


class _DiscoveryTracker:
    """Serializes discovery runs (loop and readiness-triggered) and keeps the CRD availability."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.interval_seconds = float(getattr(runtime, "interval_seconds", 300.0))
        self.availability: Optional[dict] = None
        self._lock = asyncio.Lock()

    async def run_once(self):
        async with self._lock:
            result = await self.runtime.run_once()
        availability = getattr(result, "availability", None)
        if availability is not None:
            self.availability = dict(availability)
        return result


def _create_automation_readiness(cfg, db_manager, plan_runtime, discovery: "_DiscoveryTracker"):
    """The AutomationReadiness reconciler (F12): capability activation without a restart."""
    import os

    from ..automation.readiness import AutomationReadiness, renderer_status_reader
    from ..storage.automation_capability_repository import AutomationCapabilityRepository

    try:
        repository = getattr(plan_runtime, "gitops_repository", None)
        if repository is None:
            from ..automation.plan_runtime import _make_gitops_repository

            repository = _make_gitops_repository(db_manager, cfg.DB_TYPE)
        return AutomationReadiness(
            gitops_repository=repository,
            capability_store=AutomationCapabilityRepository(db_manager),
            cluster=cfg.CLUSTER_NAME or "default",
            config=cfg,
            discovery_status=lambda: discovery.availability,
            request_discovery=discovery.run_once,
            renderer_status=renderer_status_reader(os.getenv("GITOPS_RENDERER_URL", "")),
        )
    except Exception as error:
        logger.warning("Automation readiness is unavailable (%s).", type(error).__name__)
        return None


def _create_session_purge(db_manager):
    """Daily purge of expired sessions (F14), moved from every API replica to the controller (M8)."""
    try:
        from ..auth.lifecycle import purge_sessions_once
        from ..auth.service import build_auth_service

        service = build_auth_service(db_manager)
    except Exception as error:
        logger.warning("Session purge is unavailable (%s).", type(error).__name__)
        return None

    async def session_purge() -> None:
        await purge_sessions_once(service)

    return session_purge


# Timeouts bound a wedged tick; jobs that hold their own leases (collection
# watermark, scan runs, plan operations) are not cancelled by the registry.
_TIMEOUT_SHORT = 10 * 60.0
_TIMEOUT_LONG = 30 * 60.0
_STANDBY_RECHECK_SECONDS = 2.0


def _register_controller_loops(
    registry,
    cfg,
    *,
    should_run,
    initial_work: "_InitialControllerWork",
    discovery_runtime,
    sync_runtime=None,
    automation_controller=None,
    plan_executor=None,
    session_purge=None,
    readiness=None,
) -> None:
    """Declare every periodic controller job in the loop registry (M8).

    Cadences, skip-initial behaviour and leader gating are those the jobs had under
    the previous scheduler; only the scheduling moved.
    """
    from ..core.loops import LoopSpec
    from ..core.optimization.scan_service import RECOMMENDATION_SCAN_POLL_INTERVAL
    from ..core.scheduler import parse_interval_seconds

    step = parse_interval_seconds(cfg.PROMETHEUS_QUERY_RANGE_STEP)

    async def recommendation_scan() -> None:
        if not initial_work.done:
            logger.debug("Recommendation scan poll waits for the initial controller work.")
            return
        await run_recommendation_scan(should_continue=should_run)

    specs = [
        # Fire right after each window closes (step boundary + safety lag); the
        # job itself catches up every window missed since the watermark.
        LoopSpec(
            "collection",
            scheduled_write_metrics,
            step,
            align_offset_seconds=_collection_offset_seconds(cfg),
            description="Prometheus/OpenCost collection, Impact Model allocation and metric writes.",
        ),
        LoopSpec(
            "carbon-intensity",
            collect_carbon_intensity_for_all_zones,
            3600,
            timeout_seconds=_TIMEOUT_LONG,
            description="Grid intensity history and combined-metric recompute.",
        ),
        LoopSpec(
            "node-analysis",
            analyze_nodes,
            parse_interval_seconds(cfg.NODE_ANALYSIS_INTERVAL),
            timeout_seconds=_TIMEOUT_SHORT,
            description="Node inventory refresh.",
        ),
        LoopSpec(
            "savings-attribution",
            attribute_recommendation_savings,
            step,
            timeout_seconds=_TIMEOUT_LONG,
            description="Hourly savings accrual of applied recommendations (C0.7).",
        ),
        LoopSpec(
            "recommendation-lifecycle",
            run_recommendation_lifecycle,
            parse_interval_seconds(getattr(cfg, "RECOMMENDATION_LIFECYCLE_INTERVAL", "5m")),
            timeout_seconds=_TIMEOUT_LONG,
            description="TTL expiry, apply detection, verification and change measurement.",
        ),
        LoopSpec(
            "recommendation-scan",
            recommendation_scan,
            parse_interval_seconds(RECOMMENDATION_SCAN_POLL_INTERVAL),
            description="Scheduled scans and API-queued optimization runs (E1).",
        ),
        LoopSpec(
            "metrics-compression",
            compress_metrics,
            3600,
            timeout_seconds=3600,
            description="Hourly aggregation and pruning of raw metrics.",
        ),
        LoopSpec(
            "dashboard-summary",
            refresh_dashboard_summary,
            3600,
            timeout_seconds=_TIMEOUT_LONG,
            description="Pre-computed dashboard summary refresh.",
        ),
        LoopSpec(
            "gitops-discovery",
            discovery_runtime.run_once,
            float(getattr(discovery_runtime, "interval_seconds", 300.0)),
            run_immediately=True,
            jitter=0.0,
            max_backoff_multiplier=1,
            standby_recheck_seconds=_STANDBY_RECHECK_SECONDS,
            timeout_seconds=_TIMEOUT_SHORT,
            description="Argo CD/Flux discovery and workload bindings.",
        ),
    ]
    if readiness is not None:
        from ..automation.readiness import READINESS_INTERVAL_SECONDS

        specs.append(
            LoopSpec(
                "automation-readiness",
                readiness.run_once,
                READINESS_INTERVAL_SECONDS,
                run_immediately=True,
                jitter=0.0,
                max_backoff_multiplier=2,
                standby_recheck_seconds=_STANDBY_RECHECK_SECONDS,
                timeout_seconds=_TIMEOUT_SHORT,
                description="Capability activation: provider inference, credentials, allowlist, delivery (F12).",
            )
        )
    if sync_runtime is not None:
        specs.append(
            LoopSpec(
                "gitops-sync",
                sync_runtime.run_once,
                float(getattr(sync_runtime, "interval_seconds", 30.0)),
                run_immediately=True,
                jitter=0.0,
                max_backoff_multiplier=1,
                standby_recheck_seconds=_STANDBY_RECHECK_SECONDS,
                timeout_seconds=_TIMEOUT_SHORT,
                description="Sync observation of merged plans and PR status comments.",
            )
        )
    if automation_controller is not None:
        from ..automation.controller import AUTOMATION_CONTROLLER_INTERVAL_SECONDS

        specs.append(
            LoopSpec(
                "automation-controller",
                automation_controller.tick,
                AUTOMATION_CONTROLLER_INTERVAL_SECONDS,
                run_immediately=True,
                standby_recheck_seconds=_STANDBY_RECHECK_SECONDS,
                timeout_seconds=_TIMEOUT_SHORT,
                description="Webhook outbox, PR polling, branch cleanup, plan expiry and retention (one DB lease).",
            )
        )
    if plan_executor is not None:
        from ..automation.controller import PLAN_EXECUTOR_INTERVAL_SECONDS

        specs.append(
            LoopSpec(
                "plan-executor",
                plan_executor.tick,
                PLAN_EXECUTOR_INTERVAL_SECONDS,
                run_immediately=True,
                standby_recheck_seconds=_STANDBY_RECHECK_SECONDS,
                on_standby=plan_executor.standby,
                description="Claims and executes queued plan operations (operation leases).",
            )
        )
    if session_purge is not None:
        specs.append(
            LoopSpec(
                "session-purge",
                session_purge,
                86_400,
                run_immediately=True,
                standby_recheck_seconds=60.0,
                timeout_seconds=_TIMEOUT_SHORT,
                description="Deletes expired and long-revoked browser sessions (F14).",
            )
        )
    for spec in specs:
        registry.register(spec)


async def _async_start(last: Optional[str]):
    cfg = get_config()
    from ..utils.log import configure_logging

    configure_logging(level=cfg.LOG_LEVEL, log_format=cfg.LOG_FORMAT)
    import structlog

    structlog.contextvars.bind_contextvars(cluster=cfg.CLUSTER_NAME or "default")
    logger.info("🚀 Initializing GreenKube (Async)...")

    # Establish the database connection and run schema migrations eagerly.
    # This must happen before any scheduled task runs to guarantee that all
    # tables exist.  Previously this was only done for SQLite; PostgreSQL
    # relied on lazy initialization which caused race conditions where tasks
    # would query tables before setup_postgres() had created them.
    from ..core.db import get_db_manager

    await get_db_manager().connect()
    logger.info("✅ Database connection successful and schema is ready (%s).", cfg.DB_TYPE)

    from ..automation.plan_runtime import initialize_gitops_provider_configs

    await initialize_gitops_provider_configs(db_manager=get_db_manager())

    from ..gitops.discovery.runtime import create_gitops_discovery_runtime

    discovery_runtime = _DiscoveryTracker(await create_gitops_discovery_runtime(db_manager=get_db_manager()))
    from ..core.loops import LoopRegistry

    controller_stop_event = asyncio.Event()
    registry = LoopRegistry(role="controller")
    leader_election_task = None
    initial_work_task = None
    processor = None
    plan_executor = None
    metrics_endpoint = None
    try:
        # Serve the controller's own telemetry (loops, scans, verification, collection,
        # provenance mix; A12, BUG-14, M8) and loop health on every replica, leader or standby.
        from .controller_metrics import start_controller_metrics_server

        metrics_endpoint = await start_controller_metrics_server(
            getattr(cfg, "CONTROLLER_METRICS_PORT", 0), health=registry
        )
        leader_election = _create_controller_leader_election(cfg)
        if leader_election is None:
            should_run = None
        else:
            await leader_election.try_acquire_or_renew()

            from ..core.leader_election import LeaderGate

            should_run = LeaderGate(leader_election)

            leader_election_task = asyncio.create_task(
                leader_election.run(controller_stop_event),
                name="controller-leader-election",
            )
        registry.set_leader(should_run)

        plan_runtime = await _create_controller_plan_runtime(get_db_manager())
        readiness = _create_automation_readiness(cfg, get_db_manager(), plan_runtime, discovery_runtime)
        sync_runtime = None
        automation_controller = None
        if plan_runtime is not None:
            from ..automation.controller import PlanExecutor, create_automation_controller

            sync_runtime = _create_gitops_sync_runtime(plan_runtime, get_db_manager())
            # Always on (F12): the readiness reconciler switches delivery and PR tracking
            # on and off every minute, without a restart.
            delivery_active = (lambda: readiness.delivery_active) if readiness is not None else (lambda: False)
            tracking_active = (lambda: readiness.tracking_active) if readiness is not None else (lambda: False)
            automation_controller = create_automation_controller(plan_runtime, delivery_enabled=tracking_active)
            plan_executor = PlanExecutor(plan_runtime.worker, active=delivery_active)
            logger.info("Automation controller loop enabled; delivery activates with its prerequisites (F12).")

        # Resolve the processor at the composition root before workers are
        # scheduled. This makes collector/repository wiring fail fast and keeps
        # scheduled jobs focused on vertical orchestration.
        processor = get_data_processor()
        # The recommendation scan poll stays idle until the initial controller work
        # (collection, then one forced scan) has finished.
        initial_work = _InitialControllerWork()

        async def initial_recommendation_scan() -> None:
            # Scan the data the initial collection just wrote, even when a
            # scan (e.g. from the previous pod) already covers the interval.
            await run_recommendation_scan(should_continue=should_run, force=True)

        _register_controller_loops(
            registry,
            cfg,
            should_run=should_run,
            initial_work=initial_work,
            discovery_runtime=discovery_runtime,
            sync_runtime=sync_runtime,
            automation_controller=automation_controller,
            plan_executor=plan_executor,
            session_purge=_create_session_purge(get_db_manager()),
            readiness=readiness,
        )
        logger.info("📈 Starting %d controller loops: %s", len(registry.names), ", ".join(registry.names))
        registry.start()
        logger.info("\nGreenKube is running. Press CTRL+C to exit.")

        initial_tasks = (
            (collect_carbon_intensity_for_all_zones, (), {}),
            (analyze_nodes, (), {}),
            (async_write_combined_metrics_to_database, (), {"last": last}),
            (attribute_recommendation_savings, (), {}),
            (run_recommendation_lifecycle, (), {}),
            (compress_metrics, (), {}),
            (refresh_dashboard_summary, (), {}),
            (initial_recommendation_scan, (), {}),
        )
        if should_run is None:
            logger.info("Running initial controller work...")
            try:
                for callback, args, kwargs in initial_tasks:
                    if not await _run_initial_controller_task(callback, should_run, *args, **kwargs):
                        break
                else:
                    logger.info("Initial collection complete.")
            finally:
                initial_work.mark_done()
        else:
            logger.info("Initial controller work will start after Lease acquisition.")
            initial_work_task = asyncio.create_task(
                _run_deferred_initial_controller_work(
                    initial_tasks,
                    should_run,
                    controller_stop_event,
                    on_finished=initial_work.mark_done,
                ),
                name="controller-initial-work",
            )

        def handle_sig():
            logger.info("\n🛑 Received shutdown signal...")
            controller_stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, handle_sig)

        await controller_stop_event.wait()
    finally:
        controller_stop_event.set()
        try:
            await registry.stop()
        finally:
            tasks = [task for task in (leader_election_task, initial_work_task) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if plan_executor is not None:
                plan_executor.close()
            if processor is not None:
                await processor.close()
            from .controller_metrics import stop_controller_metrics_server

            await stop_controller_metrics_server(metrics_endpoint)
            logger.info("🛑 Shutting down GreenKube service gracefully.")


@app.callback(invoke_without_command=True)
def start(
    ctx: typer.Context,
    last: Annotated[
        Optional[str],
        typer.Option("--last", help="Time range to collect (e.g., '10min', '2h', '7d', '3w', '1m' for month)."),
    ] = None,
) -> None:
    """
    Initialize the database (if needed) and start the scheduler loop.
    """
    if ctx.invoked_subcommand is not None:
        return

    try:
        asyncio.run(_async_start(last))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("❌ An unexpected error occurred during startup: %s", e)
        logger.error("Startup failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)
