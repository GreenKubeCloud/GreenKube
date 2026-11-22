# src/greenkube/api/routers/optimization.py
"""Optimization run requests and history (E1, E10).

The API never runs a recommendation scan itself. ``POST /optimization/runs``
queues a run (deduplicated while one is pending for the same scope); the
leader controller claims and executes it, then the run records per-source
health and the diff against the previous active set.

Endpoints:
  POST /optimization/runs            - Queue a scan (202 {"run_id", "status"})
  GET  /optimization/runs            - Recent runs, newest first (?limit=)
  GET  /optimization/runs/{run_id}   - One run's status and outcome
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response, status

from greenkube.api.dependencies import get_authenticated_actor, validate_namespace
from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunAccepted,
    OptimizationRunRequest,
    OptimizationRunResponse,
    OptimizationRunStatus,
    OptimizationRunTrigger,
)
from greenkube.storage.base_optimization_run_repository import MAX_RUN_LIST_LIMIT, OptimizationRunRepository

logger = logging.getLogger(__name__)

router = APIRouter()

#: Response header naming the run queued by a deprecated ``refresh=true`` GET.
RUN_ID_HEADER = "X-GreenKube-Run-Id"
#: A ``refresh=true`` GET reuses a pending run, or one that finished this
#: recently, for its scope instead of queuing another scan.
REFRESH_REUSE_WINDOW = timedelta(minutes=5)
#: A failed run is reused only this long, so a refresh retries soon after a
#: failure without page loads chaining back-to-back scans.
REFRESH_FAILURE_BACKOFF = timedelta(minutes=1)


async def get_optimization_run_repository() -> OptimizationRunRepository:
    """Provide the optimization run repository for the configured backend."""
    from greenkube.core.optimization.scan_service import build_optimization_run_repository

    return build_optimization_run_repository()


async def enqueue_optimization_run(
    run_repo: OptimizationRunRepository,
    *,
    namespace: Optional[str],
    trigger: OptimizationRunTrigger,
    requested_by: Optional[str],
):
    """Queue a run for the controller; returns ``(run, created)``."""
    return await run_repo.enqueue(
        namespace=namespace,
        trigger=trigger,
        requested_at=datetime.now(timezone.utc),
        requested_by=requested_by,
    )


async def enqueue_refresh(
    run_repo: OptimizationRunRepository,
    response: Response,
    *,
    namespace: Optional[str],
    requested_by: Optional[str],
) -> Optional[int]:
    """Deprecated ``refresh=true`` semantics: queue a run, expose its id, never scan inline.

    A run covering the scope that is queued, running or finished within
    :data:`REFRESH_REUSE_WINDOW` is reused, so page loads that still send
    ``refresh=true`` do not chain scans. A failed run is reused only for
    :data:`REFRESH_FAILURE_BACKOFF`; after that the refresh queues a retry.
    Failures are logged and swallowed so the read still returns current data.
    """
    try:
        now = datetime.now(timezone.utc)
        run = await run_repo.find_recent_covering(namespace=namespace, completed_after=now - REFRESH_REUSE_WINDOW)
        if run is not None and _failed_before(run, now - REFRESH_FAILURE_BACKOFF):
            run = None
        if run is None:
            run, _ = await enqueue_optimization_run(
                run_repo,
                namespace=namespace,
                trigger=OptimizationRunTrigger.REFRESH,
                requested_by=requested_by,
            )
    except Exception as exc:
        logger.warning("Could not queue a recommendation refresh (%s).", type(exc).__name__)
        return None
    if run.id is None:
        return None
    response.headers[RUN_ID_HEADER] = str(run.id)
    return run.id


def _failed_before(run: OptimizationRun, cutoff: datetime) -> bool:
    """Whether ``run`` failed before ``cutoff`` (a stale failure is not worth reusing)."""
    if run.status != OptimizationRunStatus.FAILED or run.completed_at is None:
        return False
    completed_at = run.completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return completed_at < cutoff


@router.post(
    "/optimization/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OptimizationRunAccepted,
    responses={400: {"description": "Invalid namespace."}},
)
async def request_optimization_run(
    response: Response,
    body: Optional[OptimizationRunRequest] = Body(default=None),
    run_repo: OptimizationRunRepository = Depends(get_optimization_run_repository),
    actor: str = Depends(get_authenticated_actor),
) -> OptimizationRunAccepted:
    """Queue a recommendation scan for the controller.

    While a run is still queued for the same scope (or a cluster-wide run is
    queued), its id is returned instead of queuing a duplicate. Poll
    ``GET /optimization/runs/{run_id}`` until the status is terminal.
    """
    namespace = validate_namespace(body.namespace if body is not None else None)
    run, created = await enqueue_optimization_run(
        run_repo,
        namespace=namespace,
        trigger=OptimizationRunTrigger.MANUAL,
        requested_by=actor,
    )
    if run.id is None:
        raise HTTPException(status_code=500, detail="The optimization run could not be queued.")
    response.headers["Location"] = f"/api/v1/optimization/runs/{run.id}"
    if not created:
        logger.debug("Optimization run request deduplicated into queued run %s.", run.id)
    return OptimizationRunAccepted(run_id=run.id, status=run.status)


@router.get("/optimization/runs", response_model=List[OptimizationRunResponse])
async def list_optimization_runs(
    limit: int = Query(20, ge=1, le=MAX_RUN_LIST_LIMIT, description="Number of runs to return, newest first."),
    run_repo: OptimizationRunRepository = Depends(get_optimization_run_repository),
) -> List[OptimizationRunResponse]:
    """Return recent optimization runs with per-source health and diff counts."""
    runs = await run_repo.list_runs(limit=limit)
    return [OptimizationRunResponse.from_run(run) for run in runs if run.id is not None]


@router.get(
    "/optimization/runs/{run_id}",
    response_model=OptimizationRunResponse,
    responses={404: {"description": "Unknown optimization run."}},
)
async def get_optimization_run(
    run_id: int = Path(..., ge=1),
    run_repo: OptimizationRunRepository = Depends(get_optimization_run_repository),
) -> OptimizationRunResponse:
    """Return one optimization run's status and outcome."""
    run = await run_repo.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Optimization run {run_id} not found.")
    return OptimizationRunResponse.from_run(run)
