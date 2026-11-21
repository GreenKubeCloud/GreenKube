# src/greenkube/cli/utils.py
import logging
from datetime import timedelta
from typing import Optional

import typer

from ..core.cluster_identity import (
    InstallationIdentityError,
    adopt_legacy_collection_keys,
    cached_installation_identity,
    resolve_installation_identity,
)
from ..core.collection import METRICS_WATERMARK_SOURCE, CollectionReport, MetricsCollectionJob
from ..core.config import get_config
from ..core.factory import get_combined_metrics_repository, get_processor
from ..storage.base_collection_watermark_repository import create_collection_watermark_repository
from ..storage.base_collection_window_repository import create_collection_window_repository
from ..storage.installation_repository import create_installation_repository
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


def get_collection_watermark_repository():
    """Return the watermark repository for the configured database."""
    from ..core.db import get_db_manager

    return create_collection_watermark_repository(get_db_manager(), get_config().DB_TYPE)


def get_collection_window_repository():
    """Return the per-step collection record repository for the configured database (COL-04)."""
    from ..core.db import get_db_manager

    return create_collection_window_repository(get_db_manager(), get_config().DB_TYPE)


async def resolve_collection_cluster_uid() -> str:
    """Installation identity keying the collection watermark and records (COL-02, STO-09).

    Resolved once per process: the stored identity is checked against the live
    kube-system UID (a mismatch raises unless ``ALLOW_CLUSTER_UID_CHANGE``), and
    watermarks and records written under a cluster or node name are adopted.

    Raises:
        InstallationIdentityError: the identity cannot be read or belongs to another cluster.
    """
    cached = cached_installation_identity()
    if cached is not None:
        return cached.cluster_uid
    from ..core.db import get_db_manager

    cfg = get_config()
    repo = create_installation_repository(get_db_manager(), cfg.DB_TYPE)
    identity = await resolve_installation_identity(
        repo, cfg.CLUSTER_NAME or None, allow_change=bool(getattr(cfg, "ALLOW_CLUSTER_UID_CHANGE", False))
    )
    await adopt_legacy_collection_keys(repo, identity.cluster_uid, METRICS_WATERMARK_SOURCE)
    return identity.cluster_uid


async def write_combined_metrics_to_database(
    last: Optional[str] = None, force: bool = False
) -> Optional[CollectionReport]:
    """Collect every missing window since the watermark (or the explicit ``--last`` range) and store it.

    Scheduled runs (``last=None``) catch up from the collection watermark,
    oldest window first, and advance it only after each window is written
    (A2).  ``--last`` re-collects the given range with an idempotent upsert;
    with ``force`` it replaces the stored rows even when the new pass looks
    worse or prices them with a lower-ranked cost source (STO-01).
    """
    if force and not last:
        logger.error("--force applies to an explicit --last re-collection only.")
        return None
    logger.info("--- Starting combined metrics collection task ---")
    lookback = parse_last_duration(last) if last else None
    try:
        cluster_uid = await resolve_collection_cluster_uid()
    except InstallationIdentityError as e:
        logger.error("Metrics collection does not start: %s", e)
        return None
    try:
        processor = get_processor()
        job = MetricsCollectionJob(
            processor=processor,
            metrics_repository=get_combined_metrics_repository(),
            watermark_repository=get_collection_watermark_repository(),
            config=get_config(),
            cluster_uid=cluster_uid,
            window_repository=get_collection_window_repository(),
        )
    except Exception as e:
        logger.error("Failed to initialize components for combined metrics collection: %s", e)
        return None

    try:
        report = await job.run(last=lookback, force=force)
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
        return report
    finally:
        await processor.close()
        logger.info("--- Finished combined metrics collection task ---")
