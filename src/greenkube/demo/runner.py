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

import uvicorn

from greenkube.core.config import get_config
from greenkube.demo.data_generator import (
    DEMO_ZONE,
    generate_carbon_intensity_history,
    generate_combined_metrics,
    generate_node_snapshots,
    generate_recommendations,
)

logger = logging.getLogger(__name__)


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
    counts["carbon_intensity"] = await repo.save_history(history, zone=DEMO_ZONE)

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
    from greenkube.core.factory import get_recommendation_repository

    reco_repo = get_recommendation_repository()
    recommendations = generate_recommendations()
    counts["recommendations"] = await reco_repo.save_recommendations(recommendations)

    # 5. Compress older raw metrics into hourly buckets so aggregate_timeseries
    #    can query both the raw and hourly tables consistently across all windows.
    logger.info("🗜️  Compressing historical metrics into hourly buckets...")
    from greenkube.core.db import get_db_manager
    from greenkube.core.metrics_compressor import MetricsCompressor

    compressor = MetricsCompressor(get_db_manager())
    compression_stats = await compressor.run()
    counts["hourly_compressed"] = compression_stats.get("rows_compressed", 0)

    # 6. Pre-compute dashboard summary and timeseries cache for all windows
    logger.info("⚡ Pre-computing dashboard summary and timeseries cache...")
    from greenkube.core.factory import get_summary_repository, get_timeseries_cache_repository
    from greenkube.core.summary_refresher import SummaryRefresher

    refresher = SummaryRefresher(
        metrics_repo=combined_repo,
        summary_repo=get_summary_repository(),
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
        # Clean up
        from greenkube.core.db import get_db_manager

        await get_db_manager().close()
        logger.info("🧹 Demo database at %s can be manually deleted.", tmp_dir)
        logger.info("👋 GreenKube Demo stopped. Thanks for trying GreenKube!")
