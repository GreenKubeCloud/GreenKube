# src/greenkube/cli/utils.py
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import typer

from ..core.config import config
from ..core.factory import get_processor, get_repository
from ..models.metrics import CombinedMetric
from ..utils.date_utils import parse_duration

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


def get_report_time_range(last: Optional[str] = None) -> tuple[datetime, datetime]:
    """
    Calculates the start and end time for a report.
    If 'last' is provided, start = end - duration.
    Otherwise, defaults to last 24 hours.
    """
    end = datetime.now(timezone.utc)
    if last:
        start = end - parse_last_duration(last)
    else:
        start = end - timedelta(days=1)
    return start, end


def get_normalized_window() -> tuple[datetime, datetime]:
    """
    Calculates a consistent, non-overlapping query window based on the configured step.
    The window is aligned to UTC midnight.
    """
    step_str = config.PROMETHEUS_QUERY_RANGE_STEP
    match = re.match(r"^(\d+)([smh])$", step_str.lower())
    if not match:
        raise ValueError(f"Unsupported PROMETHEUS_QUERY_RANGE_STEP format: '{step_str}'. Use 's', 'm', or 'h'.")

    value, unit = int(match.group(1)), match.group(2)
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
        repository = get_repository()
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

        saved_count = await repository.write_combined_metrics(combined_data)
        logger.info("Successfully saved %s new combined metrics records.", saved_count)

    except Exception as e:
        logger.exception("Failed to process and save combined metrics data: %s", e)
    finally:
        if "processor" in locals() and processor:
            await processor.close()

    logger.info("--- Finished combined metrics collection task ---")


async def read_combined_metrics_from_database(
    start: datetime, end: datetime, namespace: Optional[str] = None
) -> List[CombinedMetric]:
    """
    Reads combined metrics from the database within a given time range and optional namespace.
    """
    logger.info("--- Reading combined metrics from %s to %s ---", start, end)
    try:
        repository = get_repository()
        data = await repository.read_combined_metrics(start_time=start, end_time=end)
        logger.info("Found %d combined metrics records.", len(data))

        # Filter by namespace if provided
        if namespace:
            data = [item for item in data if item.namespace == namespace]

        return data
    except Exception as e:
        logger.error("Failed to read combined metrics from database: %s", e)
        return []
