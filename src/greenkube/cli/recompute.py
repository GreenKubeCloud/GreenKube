# src/greenkube/cli/recompute.py
"""``greenkube recompute``: re-allocate raw metric rows with the current Impact Model (roadmap B8)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import typer

from ..utils.time import parse_duration, parse_iso_date

logger = logging.getLogger(__name__)


def parse_since(value: str, now: datetime) -> datetime:
    """A duration before ``now`` (``7d``, ``12h``) or an ISO-8601 timestamp."""
    try:
        return now - parse_duration(value)
    except ValueError:
        parsed = parse_iso_date(value)
        if parsed is None:
            raise typer.BadParameter(f"'{value}' is neither a duration (7d, 12h) nor an ISO-8601 timestamp.")
        return parsed


async def _run(start: datetime, end: datetime, *, only_outdated: bool, dry_run: bool) -> str:
    from ..core.config import get_config
    from ..core.db import get_db_manager
    from ..core.factory import get_combined_metrics_repository, get_node_repository
    from ..impact.recompute import MetricRecomputer

    manager = get_db_manager()
    await manager.connect()
    try:
        recomputer = MetricRecomputer(get_config(), get_combined_metrics_repository(), get_node_repository())
        report = await recomputer.run(start, end, only_outdated=only_outdated, dry_run=dry_run)
        return report.summary()
    finally:
        await manager.close()


def recompute(
    since: str = typer.Option(
        ...,
        "--since",
        help="Start of the range: a duration before now (7d, 12h) or an ISO-8601 timestamp.",
    ),
    until: Optional[str] = typer.Option(None, "--until", help="End of the range (exclusive); default: now."),
    all_rows: bool = typer.Option(
        False,
        "--all",
        help="Recompute every row in the range, not only rows written by another model version.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change without writing."),
):
    """Re-allocate raw metric rows within retention with the current Impact Model.

    Energy, CO2e, embodied emissions and modeled costs are recomputed from the
    stored requests, usage, node and intensity; observed OpenCost costs and
    hourly aggregates are kept as they are.
    """
    now = datetime.now(timezone.utc)
    start = parse_since(since, now)
    end = parse_since(until, now) if until else now
    if end <= start:
        raise typer.BadParameter("--until must be after --since.")
    summary = asyncio.run(_run(start, end, only_outdated=not all_rows, dry_run=dry_run))
    typer.echo(summary)
