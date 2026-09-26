# src/greenkube/cli/recommend.py
"""
Implements the `recommend` command for the GreenKube CLI.

Uses the unified optimization engine (all recommendation types) by reading
stored metrics from the database, matching the behaviour of the API endpoint.
"""

import asyncio
import logging
import traceback
from typing import Optional

import typer
from typing_extensions import Annotated

from ..core.factory import get_combined_metrics_repository, get_node_repository
from ..core.optimization.context_builder import ContextBuilder
from ..core.optimization.engine import OptimizationEngine
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
    fail_on_recommendations: Annotated[
        bool,
        typer.Option(
            "--fail-on-recommendations",
            help="Exit with code 1 if any recommendations are found. Useful for CI/CD policy gates.",
        ),
    ] = False,
):
    """
    Analyzes data and provides optimization recommendations.

    By default, reads stored metrics from the database (same source as the
    API). Use --live to run the full collection pipeline in real-time.
    """
    if ctx.invoked_subcommand is not None:
        return

    logger.info("Initializing GreenKube optimization engine...")

    async def _recommend_async():
        processor = None
        try:
            engine = OptimizationEngine()
            builder = ContextBuilder()

            if live:
                from ..core.factory import get_processor

                processor = get_processor()
                logger.info("Running the data processing pipeline (live mode)...")
                combined_data = await processor.run()
                node_repo = get_node_repository()
                context = await builder.build_from_metrics(combined_data, node_repo, namespace=namespace)
            else:
                logger.info("Reading stored metrics from database...")
                repository = get_combined_metrics_repository()
                node_repo = get_node_repository()
                context = await builder.build(repository, node_repo, namespace=namespace)

            if not context.metrics:
                if namespace:
                    logger.warning("No data found for namespace '%s'.", namespace)
                else:
                    logger.warning("No combined data available. Cannot generate recommendations.")
                return

            recommendations = await engine.generate(context)

            logger.info("Found %d recommendations.", len(recommendations))

            console_reporter = ConsoleReporter()
            console_reporter.report_recommendations(recommendations)

            if fail_on_recommendations and recommendations:
                logger.warning(
                    "CI/CD gate: %d recommendation(s) found. Exiting with code 1 (--fail-on-recommendations).",
                    len(recommendations),
                )
                raise typer.Exit(code=1)

        except typer.Exit:
            raise
        except Exception as e:
            logger.error("An error occurred during recommendation generation: %s", e)
            logger.error("Recommendation generation failed: %s", traceback.format_exc())
            raise typer.Exit(code=1)
        finally:
            if processor is not None:
                await processor.close()
            from ..core.db import get_db_manager

            await get_db_manager().close()

    try:
        asyncio.run(_recommend_async())
    except typer.Exit:
        raise
    except Exception as e:
        logger.error("An unexpected error occurred: %s", e)
        raise typer.Exit(code=1)
