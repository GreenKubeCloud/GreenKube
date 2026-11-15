# src/greenkube/cli/utils.py
import logging
from datetime import timedelta
from typing import Optional

import typer

from ..core.collection import CollectionReport, MetricsCollectionJob
from ..core.config import get_config
from ..core.factory import get_combined_metrics_repository, get_processor
from ..storage.base_collection_watermark_repository import create_collection_watermark_repository
from ..storage.base_collection_window_repository import create_collection_window_repository
from ..utils.date_utils import parse_duration

try:
    from ..api.metrics_endpoint import update_cluster_metrics
except Exception:  # pragma: no cover – optional dependency
    update_cluster_metrics = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def parse_last_duration(last: str) -> timedelta:
    """Parses a duration string (e.g., '3h', '7d', '2w') into a timedelta.

    Delegates to :func:`greenkube.utils.date_utils.parse_duration` and wraps
    the :class:`ValueError` into a :class:`typer.BadParameter` for CLI use.
    """
    try:
        return parse_duration(last)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def get_collection_watermark_repository():
    """Return the watermark repository for the configured database."""
    from ..core.db import get_db_manager

    return create_collection_watermark_repository(get_db_manager(), get_config().DB_TYPE)


def get_collection_window_repository():
    """Return the per-step collection record repository for the configured database (COL-04)."""
    from ..core.db import get_db_manager

    return create_collection_window_repository(get_db_manager(), get_config().DB_TYPE)


async def write_combined_metrics_to_database(last: Optional[str] = None) -> Optional[CollectionReport]:
    """Collect every missing window since the watermark (or the explicit ``--last`` range) and store it.

    Scheduled runs (``last=None``) catch up from the collection watermark,
    oldest window first, and advance it only after each window is written
    (A2).  ``--last`` re-collects the given range with an idempotent upsert.
    """
    logger.info("--- Starting combined metrics collection task ---")
    lookback = parse_last_duration(last) if last else None
    try:
        processor = get_processor()
        job = MetricsCollectionJob(
            processor=processor,
            metrics_repository=get_combined_metrics_repository(),
            watermark_repository=get_collection_watermark_repository(),
            config=get_config(),
            window_repository=get_collection_window_repository(),
        )
    except Exception as e:
        logger.error("Failed to initialize components for combined metrics collection: %s", e)
        return None

    try:
        report = await job.run(last=lookback)
        if report.windows_collected == 0 and report.succeeded:
            logger.info("No complete collection window is pending.")
        else:
            logger.info(
                "Collected %d/%d window(s), saved %d record(s); watermark=%s.",
                report.windows_collected,
                report.windows_planned,
                report.rows_written,
                report.watermark.isoformat() if report.watermark else None,
            )

        # Update Prometheus gauges with the most recent window for Grafana scraping
        if update_cluster_metrics is not None and report.latest_rows:
            try:
                update_cluster_metrics(report.latest_rows)
            except Exception as e:
                logger.warning("Failed to update Prometheus cluster metrics: %s", e)
        return report
    finally:
        await processor.close()
        logger.info("--- Finished combined metrics collection task ---")
