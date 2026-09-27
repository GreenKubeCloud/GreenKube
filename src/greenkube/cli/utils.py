# src/greenkube/cli/utils.py
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import typer

from ..core.config import get_config
from ..core.factory import get_combined_metrics_repository, get_processor
from ..models.metrics import CombinedMetric
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


def get_normalized_window() -> tuple[datetime, datetime]:
    """
    Calculates a consistent, non-overlapping query window based on the configured step.
    The window is aligned to UTC midnight.
    """
    step_str = get_config().PROMETHEUS_QUERY_RANGE_STEP
    match = re.match(r"^(\d+)([smh])$", step_str.lower())
    if not match:
        raise ValueError(f"Unsupported PROMETHEUS_QUERY_RANGE_STEP format: '{step_str}'. Use 's', 'm', or 'h'.")

    value, unit = int(match.group(1)), match.group(2)
    if value <= 0:
        raise ValueError(f"PROMETHEUS_QUERY_RANGE_STEP must be greater than zero, got '{step_str}'.")
    if unit == "s":
        step_delta = timedelta(seconds=value)
    elif unit == "m":
        step_delta = timedelta(minutes=value)
    else:  # h
        step_delta = timedelta(hours=value)

    now = datetime.now(timezone.utc)
    total_seconds_since_midnight = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    end = now - timedelta(seconds=total_seconds_since_midnight % step_delta.total_seconds())
    return end - step_delta, end


async def write_combined_metrics_to_database(last: Optional[str] = None) -> None:
    """
    Orchestrates the collection and saving of combined metrics data, avoiding duplicates.
    """
    logger.info("--- Starting combined metrics collection task ---")
    try:
        combined_metrics_repo = get_combined_metrics_repository()
        processor = get_processor()
    except Exception as e:
        logger.error("Failed to initialize components for combined metrics collection: %s", e)
        return

    if last:
        # For ad-hoc runs with --last, use the exact time for responsiveness.
        end = datetime.now(timezone.utc)
        start = end - parse_last_duration(last)
    else:
        # For scheduled runs, use the normalized window.
        start, end = get_normalized_window()

    try:
        combined_data: List[CombinedMetric] = await processor.run_range(start=start, end=end)
        if not combined_data:
            logger.info("No new combined metrics data to save.")
            return

        saved_count = await combined_metrics_repo.write_combined_metrics(combined_data)
        logger.info("Successfully saved %s new combined metrics records.", saved_count)

        # Update Prometheus gauges for Grafana scraping
        if update_cluster_metrics is not None:
            try:
                update_cluster_metrics(combined_data)
            except Exception as e:
                logger.warning("Failed to update Prometheus cluster metrics: %s", e)

    except Exception as e:
        logger.exception("Failed to process and save combined metrics data: %s", e)
    finally:
        if "processor" in locals() and processor:
            await processor.close()

    logger.info("--- Finished combined metrics collection task ---")
