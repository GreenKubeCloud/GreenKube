# src/greenkube/cli/utils.py
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import typer

from ..core.config import config
from ..core.factory import get_processor, get_repository
from ..models.metrics import CombinedMetric

logger = logging.getLogger(__name__)


def parse_last_duration(last: str) -> timedelta:
    """Parses a duration string (e.g., '3h', '7d', '2w') into a timedelta."""
    match = re.match(r"^(\d+)(min|[hdwmy])$", last.lower())
    if not match:
        raise typer.BadParameter(
            f"Invalid format for --last: '{last}'. Use format like '10min', '2h', '7d', '3w', '1m' (month), '1y'."
        )

    value, unit = int(match.group(1)), match.group(2)
    if unit == "min":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "w":
        return timedelta(weeks=value)
    elif unit == "m":
        # Approximate month as 30 days
        return timedelta(days=value * 30)
    elif unit == "y":
        # Approximate year as 365 days.
        return timedelta(days=value * 365)
    # This line is unreachable due to regex
    return timedelta()


def get_normalized_window() -> (datetime, datetime):
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


def write_combined_metrics_to_database(last: Optional[str] = None) -> None:
    """
    Orchestrates the collection and saving of combined metrics data, avoiding duplicates.
    """
    logger.info("--- Starting combined metrics collection task ---")
    try:
        repository = get_repository()
        processor = get_processor()
    except Exception as e:
        logger.error(f"Failed to initialize components for combined metrics collection: {e}")
        return

    if last:
        # For ad-hoc runs with --last, use the exact time for responsiveness.
        end = datetime.now(timezone.utc)
        start = end - parse_last_duration(last)
    else:
        # For scheduled runs, use the normalized window.
        start, end = get_normalized_window()

    try:
        combined_data: List[CombinedMetric] = processor.run_range(start=start, end=end)
        if not combined_data:
            logger.info("No new combined metrics data to save.")
            return

        saved_count = repository.write_combined_metrics(combined_data)
        logger.info(f"Successfully saved {saved_count} new combined metrics records.")

    except Exception as e:
        logger.error(f"Failed to process and save combined metrics data: {e}", exc_info=True)

    logger.info("--- Finished combined metrics collection task ---")


def read_combined_metrics_from_database(
    start: datetime, end: datetime, namespace: Optional[str] = None
) -> List[CombinedMetric]:
    """
    Reads combined metrics from the database within a given time range and optional namespace.
    """
    logger.info(f"--- Reading combined metrics from {start} to {end} ---")
    try:
        repository = get_repository()
        data = repository.read_combined_metrics(start=start, end=end)
        logger.info(f"Found {len(data)} combined metrics records.")

        # Filter by namespace if provided
        if namespace:
            data = [item for item in data if item.namespace == namespace]

        return data
    except Exception as e:
        logger.error(f"Failed to read combined metrics from database: {e}")
        return []
