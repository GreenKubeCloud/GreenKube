# src/greenkube/api/routers/collection.py
"""Collection coverage: which steps of a period hold known data (COL-04)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from greenkube.api.schemas import CollectionCoverageResponse, CollectionHoleResponse, CollectionStatusResponse
from greenkube.core.collection import (
    METRICS_WATERMARK_SOURCE,
    align_down,
    align_up,
    collection_cluster_key,
    collection_step,
)
from greenkube.core.config import get_config
from greenkube.storage.base_collection_watermark_repository import CollectionWatermarkRepository
from greenkube.storage.base_collection_window_repository import CollectionWindowRepository, compute_coverage
from greenkube.utils.time import ensure_utc

logger = logging.getLogger(__name__)

router = APIRouter()

#: Longest period one coverage request may span (one record per step is expanded in memory).
MAX_COVERAGE_PERIOD = timedelta(days=366)


async def get_collection_window_repository() -> CollectionWindowRepository:
    """Provide the collection window repository of the configured database."""
    from greenkube.core.db import get_db_manager
    from greenkube.storage.base_collection_window_repository import create_collection_window_repository

    return create_collection_window_repository(get_db_manager(), get_config().DB_TYPE)


async def get_collection_watermark_repository() -> CollectionWatermarkRepository:
    """Provide the collection watermark repository of the configured database."""
    from greenkube.core.db import get_db_manager
    from greenkube.storage.base_collection_watermark_repository import create_collection_watermark_repository

    return create_collection_watermark_repository(get_db_manager(), get_config().DB_TYPE)


async def collection_key() -> str:
    """Key of the collection records: the installation identity, else ``CLUSTER_NAME`` (before the first run)."""
    from greenkube.core.cluster_identity import cached_installation_identity
    from greenkube.core.db import get_db_manager
    from greenkube.storage.installation_repository import create_installation_repository

    cached = cached_installation_identity()
    if cached is not None:
        return cached.cluster_uid
    config = get_config()
    stored = await create_installation_repository(get_db_manager(), config.DB_TYPE).get()
    return stored.cluster_uid if stored is not None else collection_cluster_key(config)


@router.get("/collection/coverage", response_model=CollectionCoverageResponse)
async def collection_coverage(
    start: Optional[datetime] = Query(None, description="Period start (default: 24 h before end)."),
    end: Optional[datetime] = Query(None, description="Period end (default: now)."),
    windows: CollectionWindowRepository = Depends(get_collection_window_repository),
):
    """Coverage of ``[start, end)`` by the metric collection, read from ``collection_windows``.

    Steps are widened to the collection step. A step is covered when it was
    collected or confirmed empty; failed, gap and unknown steps are listed in
    ``holes``.
    """
    config = get_config()
    step = collection_step(config)
    period_end = align_up(ensure_utc(end) if end else datetime.now(timezone.utc), step)
    period_start = align_down(ensure_utc(start) if start else period_end - timedelta(hours=24), step)
    if period_end <= period_start:
        raise HTTPException(status_code=400, detail="end must be after start.")
    if period_end - period_start > MAX_COVERAGE_PERIOD:
        raise HTTPException(status_code=400, detail="The period may span at most 366 days.")
    cluster = await collection_key()
    records = await windows.list_overlapping(cluster, METRICS_WATERMARK_SOURCE, period_start, period_end)
    coverage = compute_coverage(records, period_start, period_end, step)
    return CollectionCoverageResponse(
        cluster=cluster,
        source=METRICS_WATERMARK_SOURCE,
        start=coverage.start,
        end=coverage.end,
        step_seconds=coverage.step_seconds,
        expected_steps=coverage.expected_steps,
        collected_steps=coverage.collected_steps,
        empty_steps=coverage.empty_steps,
        failed_steps=coverage.failed_steps,
        gap_steps=coverage.gap_steps,
        unknown_steps=coverage.unknown_steps,
        coverage_ratio=coverage.ratio,
        complete=coverage.complete,
        holes=[
            CollectionHoleResponse(start=hole.start, end=hole.end, status=hole.status, reason=hole.reason)
            for hole in coverage.holes
        ],
    )


async def collection_status(
    windows: CollectionWindowRepository, watermarks: CollectionWatermarkRepository
) -> CollectionStatusResponse:
    """Watermark, lag and the most recent gap or failed step of the metric collection."""
    cluster = await collection_key()
    watermark = await watermarks.get(cluster, METRICS_WATERMARK_SOURCE)
    hole = await windows.last_hole(cluster, METRICS_WATERMARK_SOURCE)
    now = datetime.now(timezone.utc)
    return CollectionStatusResponse(
        watermark=watermark.last_complete_end if watermark else None,
        lag_seconds=max((now - watermark.last_complete_end).total_seconds(), 0.0) if watermark else None,
        last_gap=(
            CollectionHoleResponse(start=hole.window_start, end=hole.window_end, status=hole.status, reason=hole.reason)
            if hole
            else None
        ),
    )
