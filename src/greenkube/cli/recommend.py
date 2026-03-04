# src/greenkube/cli/recommend.py
"""
Implements the `recommend` command for the GreenKube CLI.

Uses the unified ``generate_recommendations()`` engine (all 9 recommendation
types) by reading stored metrics from the database, matching the behaviour
of the API endpoint.
"""

import asyncio
import logging
import traceback
from typing import Optional

import typer
from typing_extensions import Annotated

from ..core.config import config
from ..core.factory import get_combined_metrics_repository, get_node_repository
from ..core.recommender import Recommender
from ..reporters.console_reporter import ConsoleReporter

logger = logging.getLogger(__name__)

app = typer.Typer(
    help="Analyze data and provide optimization recommendations.",
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def recommend(
    ctx: typer.Context,
    namespace: Annotated[
        Optional[str],
        typer.Option(help="Display recommendations for a specific namespace."),
    ] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Run the full processor pipeline live instead of reading from the database."),
    ] = False,
):
    """
    Analyzes data and provides optimization recommendations.

    By default, reads stored metrics from the database (same source as the
    API). Use --live to run the full collection pipeline in real-time.
    """
    if ctx.invoked_subcommand is not None:
        return

    logger.info("Initializing GreenKube Recommender...")

    async def _recommend_async():
        processor = None
        try:
            from datetime import datetime, timedelta, timezone

            if live:
                from ..core.factory import get_processor

                processor = get_processor()
                logger.info("Running the data processing pipeline (live mode)...")
                combined_data = await processor.run()
            else:
                logger.info("Reading stored metrics from database...")
                repository = get_combined_metrics_repository()
                lookback_days = config.RECOMMENDATION_LOOKBACK_DAYS
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=lookback_days)
                combined_data = await repository.read_combined_metrics(start_time=start, end_time=end)

            if not combined_data:
                logger.warning("No combined data available. Cannot generate recommendations.")
                return

            # Filter by namespace if provided
            if namespace:
                logger.info("Filtering results for namespace: %s", namespace)
                combined_data = [item for item in combined_data if item.namespace == namespace]
                if not combined_data:
                    logger.warning("No data found for namespace '%s'.", namespace)
                    return

            # Fetch node info for node-level recommendations
            node_infos = []
            try:
                node_repo = get_node_repository()
                end_ts = datetime.now(timezone.utc)
                node_infos = await node_repo.get_latest_snapshots_before(end_ts)
            except Exception as e:
                logger.warning("Could not fetch node snapshots for recommendations: %s", e)

            # Detect existing HPAs to skip redundant autoscaling recommendations
            hpa_targets = None
            try:
                from ..collectors.hpa_collector import HPACollector

                hpa_collector = HPACollector()
                hpa_targets = await hpa_collector.collect()
            except Exception as e:
                logger.warning("Could not collect HPA targets: %s. Proceeding without HPA filtering.", e)

            # Generate all recommendation types via the unified engine
            recommender = Recommender()
            recommendations = recommender.generate_recommendations(
                combined_data,
                node_infos=node_infos,
                hpa_targets=hpa_targets,
            )

            logger.info("Found %d recommendations.", len(recommendations))

            console_reporter = ConsoleReporter()
            console_reporter.report_recommendations(recommendations)

        except Exception as e:
            logger.error("An error occurred during recommendation generation: %s", e)
            logger.error("Recommendation generation failed: %s", traceback.format_exc())
            raise typer.Exit(code=1)
        finally:
            if processor is not None:
                await processor.close()
            from ..core.db import db_manager

            await db_manager.close()

    try:
        asyncio.run(_recommend_async())
    except typer.Exit:
        raise
    except Exception as e:
        logger.error("An unexpected error occurred: %s", e)
        raise typer.Exit(code=1)
