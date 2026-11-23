# src/greenkube/demo/runner.py
"""
Orchestrates the GreenKube demo mode.

Sets up an in-memory SQLite database with realistic sample data,
starts the FastAPI server, and optionally opens the browser.
"""

import asyncio
import logging
import os
import signal
import tempfile
import webbrowser
from datetime import datetime, timedelta, timezone

import uvicorn

from greenkube.core.config import get_config
from greenkube.core.savings_attributor import accrual_components
from greenkube.demo.data_generator import (
    DEMO_ZONE,
    generate_carbon_intensity_history,
    generate_combined_metrics,
    generate_node_snapshots,
    generate_recommendations,
)
from greenkube.models.savings import SavingsLedgerRecord

logger = logging.getLogger(__name__)

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


async def _backfill_demo_savings_ledger(
    reco_repo,
    savings_repo,
    *,
    cluster_name: str,
    period_seconds: int = 3600,
    chunk_size: int = 2000,
) -> int:
    """Populate the historical savings ledger for already applied demo recommendations."""
    applied_records = await reco_repo.get_applied_recommendations()
    if not applied_records:
        return 0

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    period_factor = period_seconds / _SECONDS_PER_YEAR
    pending: list[SavingsLedgerRecord] = []
    inserted = 0

    for record in applied_records:
        if not record.id or not record.applied_at:
            continue

        rec_type = record.type.value if hasattr(record.type, "value") else str(record.type)
        timestamp = record.applied_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

        while timestamp <= now:
            # The production attributor decides what each slot accrues: the projection until the
            # measurement is decided, the measured rate afterwards, nothing for an inconclusive one.
            slot_end = timestamp + timedelta(seconds=period_seconds)
            for component in accrual_components(record, at=slot_end):
                pending.append(
                    SavingsLedgerRecord(
                        recommendation_id=record.id,
                        cluster_name=cluster_name,
                        namespace=record.namespace or "",
                        recommendation_type=rec_type,
                        co2e_saved_grams=component.co2e_grams_per_year * period_factor,
                        cost_saved_dollars=component.cost_per_year * period_factor,
                        period_seconds=period_seconds,
                        timestamp=timestamp,
                        measurement_method=component.method,
                    )
                )

            if len(pending) >= chunk_size:
                inserted += await savings_repo.save_records(pending)
                pending.clear()

            timestamp += timedelta(seconds=period_seconds)

    if pending:
        inserted += await savings_repo.save_records(pending)

    return inserted


#: UID of the demo cluster's kube-system namespace (fictional): the savings ledger v2 identity.
DEMO_CLUSTER_UID = "6f1c2b9e-4d3a-4c55-9a0e-3b7d1e52c0a4"


async def _demo_cluster_uid() -> str:
    return DEMO_CLUSTER_UID


def _automation_repositories():
    """The GitOps, plan-operation and pull-request repositories of the demo database."""
    from greenkube.core.db import get_db_manager
    from greenkube.core.factory import get_pull_request_repository
    from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
    from greenkube.storage.sqlite.plan_operation_repository import SQLitePlanOperationRepository

    manager = get_db_manager()
    return SQLiteGitOpsRepository(manager), SQLitePlanOperationRepository(manager), get_pull_request_repository()


async def _project_ledger_v2() -> int:
    """Catch the hash-chained savings ledger v2 up with the v1 ledger, as the controller's ledger-chain loop does."""
    from greenkube.verify.ledger.projector import build_projector

    result = await build_projector(reader=_demo_cluster_uid).sync(full=True)
    return int(result.written)


def _write_placeholder_token(mount: str) -> None:
    from greenkube.demo.automation import DEMO_CREDENTIALS_KEY, DEMO_CREDENTIALS_SECRET

    directory = os.path.join(mount, DEMO_CREDENTIALS_SECRET)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, DEMO_CREDENTIALS_KEY), "w", encoding="utf-8") as handle:
        handle.write("demo-placeholder-token")


def _configure_demo_environment(db_path: str, port: int, no_browser: bool = False) -> None:
    """Override environment variables to run in demo mode.

    Args:
        db_path: Path to the temporary SQLite database.
        port: Port number for the API server.
        no_browser: If True, bind to 0.0.0.0 (required for kubectl port-forward).
    """
    os.environ["DB_TYPE"] = "sqlite"
    os.environ["DB_PATH"] = db_path
    # Bind to 0.0.0.0 when --no-browser is used (e.g. inside a K8s pod)
    # so that kubectl port-forward can reach the server.
    os.environ["API_HOST"] = "0.0.0.0" if no_browser else "127.0.0.1"
    os.environ["API_PORT"] = str(port)
    os.environ["GREENKUBE_API_KEY"] = ""
    os.environ["CORS_ORIGINS"] = "*"
    os.environ["LOG_LEVEL"] = "INFO"
    os.environ["DEFAULT_ZONE"] = DEMO_ZONE
    os.environ["GREENKUBE_DEMO_MODE"] = "true"

    get_config().reload()


async def _populate_database(days: int) -> dict[str, int]:
    """Create and populate the SQLite database with demo data.

    Args:
        days: Number of days of historical data to generate.

    Returns:
        A dict with counts of inserted records per category.
    """
    from greenkube.core.db import get_db_manager

    await get_db_manager().connect()

    counts: dict[str, int] = {}

    # 1. Carbon intensity history
    logger.info("📊 Generating carbon intensity history...")
    from greenkube.core.factory import get_repository

    repo = get_repository()
    history = generate_carbon_intensity_history(days=days)
    history_by_zone: dict[str, list[dict]] = {}
    for record in history:
        history_by_zone.setdefault(record["zone"], []).append(record)

    counts["carbon_intensity"] = 0
    for zone, zone_history in history_by_zone.items():
        counts["carbon_intensity"] += await repo.save_history(zone_history, zone=zone)

    # 2. Node snapshots
    logger.info("🖥️  Generating node snapshots...")
    from greenkube.core.factory import get_node_repository

    node_repo = get_node_repository()
    nodes = generate_node_snapshots(days=days)
    counts["node_snapshots"] = await node_repo.save_nodes(nodes)

    # 3. Combined metrics (the main data)
    logger.info("📈 Generating combined metrics (this may take a moment)...")
    from greenkube.core.factory import get_combined_metrics_repository

    combined_repo = get_combined_metrics_repository()
    metrics = generate_combined_metrics(days=days)
    counts["combined_metrics"] = await combined_repo.write_combined_metrics(metrics)

    # 4. Recommendations
    logger.info("💡 Generating optimization recommendations...")
    from greenkube.core.factory import get_recommendation_repository, get_savings_ledger_repository
    from greenkube.core.savings_attributor import savings_cluster_name

    reco_repo = get_recommendation_repository()
    savings_repo = get_savings_ledger_repository()
    recommendations = generate_recommendations()
    counts["recommendations"] = await reco_repo.save_recommendations(recommendations)

    logger.info("🔀 Recording GitOps automation (provider, bindings, plans, pull requests)...")
    from greenkube.demo.automation import populate_demo_automation

    gitops_repo, operations, pr_repo = _automation_repositories()
    automation = await populate_demo_automation(reco_repo, gitops_repo, operations, pr_repo, get_db_manager())
    counts.update(automation.counts)

    logger.info("💸 Backfilling historical savings ledger...")
    counts["savings_ledger"] = await _backfill_demo_savings_ledger(
        reco_repo,
        savings_repo,
        cluster_name=savings_cluster_name(),
    )

    logger.info("📏 Recording measured changes...")
    from greenkube.demo.changes import populate_demo_changes
    from greenkube.storage.change_repository import get_change_repository

    counts["measured_changes"] = await populate_demo_changes(
        reco_repo, get_change_repository(), plan_ids=automation.plan_ids
    )

    logger.info("🔗 Projecting the savings ledger v2 (hash chain)...")
    counts["ledger_v2_entries"] = await _project_ledger_v2()

    # 5. Compress older raw metrics into hourly buckets so aggregate_timeseries
    #    can query both the raw and hourly tables consistently across all windows.
    logger.info("🗜️  Compressing historical metrics into hourly buckets...")
    from greenkube.core.db import get_db_manager
    from greenkube.core.metrics_compressor import MetricsCompressor

    # Demo data is generated at once, never collected: no collection watermark bounds it.
    compressor = MetricsCompressor(get_db_manager(), collection_watermark_provider=None)
    compression_stats = await compressor.run()
    counts["hourly_compressed"] = compression_stats.get("rows_compressed", 0)

    # 6. Pre-compute dashboard summary and timeseries cache for all windows
    logger.info("⚡ Pre-computing dashboard summary and timeseries cache...")
    from greenkube.core.factory import get_summary_repository, get_timeseries_cache_repository
    from greenkube.core.summary_refresher import SummaryRefresher

    summary_repo = get_summary_repository()
    refresher = SummaryRefresher(
        metrics_repo=combined_repo,
        summary_repo=summary_repo,
        timeseries_cache_repo=get_timeseries_cache_repository(),
    )
    counts["timeseries_cache_rows"] = await refresher.run()

    return counts


async def run_demo(port: int = 8000, days: int = 30, no_browser: bool = False) -> None:
    """Run GreenKube in demo mode with pre-populated sample data.

    Args:
        port: Port for the API server (default: 8000).
        days: Number of days of historical data to generate (default: 7).
        no_browser: If True, skip opening the browser automatically.
    """
    # Create a temporary SQLite database
    tmp_dir = tempfile.mkdtemp(prefix="greenkube-demo-")
    db_path = os.path.join(tmp_dir, "demo.db")

    logger.info("🚀 Starting GreenKube Demo Mode")
    logger.info("📁 Demo database: %s", db_path)

    # Configure environment for demo
    _configure_demo_environment(db_path, port, no_browser=no_browser)
    # The demo Git provider's token Secret, mounted as in a real install. A placeholder:
    # the demo plan service is read-only, so nothing is ever sent to the (fictional) Git host.
    secret_mount = os.path.join(tmp_dir, "secrets")
    _write_placeholder_token(secret_mount)
    os.environ["GITOPS_SECRET_MOUNT"] = secret_mount

    # Clear factory caches so new config is picked up
    from greenkube.core.factory import clear_caches

    clear_caches()

    # Populate the database
    counts = await _populate_database(days)

    logger.info("✅ Demo data loaded successfully:")
    for category, count in counts.items():
        logger.info("   • %s: %d records", category.replace("_", " ").title(), count)

    # Create and configure the API app
    from greenkube.api.app import create_app

    app = create_app(use_lifespan=False)
    # Plans are readable in the demo; previews, approvals and rollbacks are refused.
    from greenkube.core.db import get_db_manager
    from greenkube.demo.automation import DemoLiveState, DemoPlanService

    gitops_repo, operations, _ = _automation_repositories()
    app.state.automation_plan_service = DemoPlanService(gitops_repo, operations)
    live_state = DemoLiveState(get_db_manager(), gitops_repo, operations)
    await live_state.refresh()
    live_task = asyncio.create_task(live_state.run())

    # Resolve listen address: 0.0.0.0 for K8s (--no-browser), localhost otherwise
    host = "0.0.0.0" if no_browser else "127.0.0.1"

    # Open browser after a short delay
    url = f"http://127.0.0.1:{port}"

    if not no_browser:

        async def _open_browser():
            await asyncio.sleep(1.5)
            logger.info("🌐 Opening browser at %s", url)
            webbrowser.open(url)

        asyncio.create_task(_open_browser())

    logger.info("")
    logger.info("=" * 60)
    logger.info("  🌿 GreenKube Demo is running!")
    logger.info("  📊 Dashboard:  %s", url)
    logger.info("  📚 API Docs:   %s/api/v1/docs", url)
    logger.info("  🔌 Metrics:    %s/api/v1/metrics?last=7d", url)
    logger.info("  💡 Recos:      %s/api/v1/recommendations", url)
    logger.info("  Press CTRL+C to stop.")
    logger.info("=" * 60)
    logger.info("")

    # Run uvicorn
    uvi_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(uvi_config)

    # Handle shutdown gracefully
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("\n🛑 Shutting down demo...")
        stop_event.set()
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await server.serve()
    finally:
        live_task.cancel()
        # Clean up
        from greenkube.core.db import get_db_manager

        await get_db_manager().close()
        logger.info("🧹 Demo database at %s can be manually deleted.", tmp_dir)
        logger.info("👋 GreenKube Demo stopped. Thanks for trying GreenKube!")
