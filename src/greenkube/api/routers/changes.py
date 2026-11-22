# src/greenkube/api/routers/changes.py
"""Applied changes and their measurements (roadmap C1, C2).

Endpoints (prefix ``/api/v2``):
  GET /changes                               - Changes, newest first (?namespace=&state=&plan_id=&limit=&cursor=)
  GET /changes/{change_id}                   - One change: state, baseline snapshot and lifecycle events
  GET /changes/{change_id}/measurements      - Decided measurements of a change, oldest first

A change is one applied pod-template revision of one workload (a merged and
synced GitOps plan, or a change detected in the cluster). Each measurement
reports signed per-quantity savings with a 90% confidence interval, the load
driver and counterfactual used, confounders and the outcome; its
``allocations`` and ``recommendations`` show how the saving is split between
the change's recommendations. Projected, measured and realized values are
never summed: every estimate states its quantity, unit and whether it is
claimed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from greenkube.api.dependencies import validate_namespace
from greenkube.api.routers.automation import external_pull_request_url
from greenkube.storage.change_repository import ChangeRepository, get_change_repository
from greenkube.storage.measurement_repository import (
    SavingsMeasurementRepository,
    get_savings_measurement_repository,
)
from greenkube.utils.time import ensure_utc
from greenkube.verify.model import BaselineSnapshot, ChangeEvent, ChangeStatus, MeasurementState

logger = logging.getLogger(__name__)


# The app-wide ``authenticate`` dependency resolves the principal of ``/api/v2``
# routes like ``/api/v1`` ones: sessions, scoped API keys (``read``) and the
# bootstrap key.
router = APIRouter()

MAX_LIMIT = 200
_CHANGE_ID = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


async def get_changes_repository() -> ChangeRepository:
    """Provide the change repository for the configured backend."""
    return get_change_repository()


async def get_measurements_repository() -> SavingsMeasurementRepository:
    """Provide the per-recommendation measurement repository."""
    return get_savings_measurement_repository()


def _change_payload(change: ChangeEvent, status: Optional[ChangeStatus]) -> dict[str, Any]:
    payload = change.model_dump(mode="json")
    payload["pr_url"] = external_pull_request_url(change.pr_url)
    payload["state"] = status.state.value if status is not None else None
    payload["measurement_sequence"] = status.sequence if status is not None else None
    payload["latest_measurement_id"] = status.latest_measurement_id if status is not None else None
    payload["post_window"] = (
        {
            "start": status.post_window_start.isoformat() if status.post_window_start else None,
            "end": status.post_window_end.isoformat() if status.post_window_end else None,
        }
        if status is not None and status.post_window_start is not None
        else None
    )
    payload["state_updated_at"] = status.updated_at.isoformat() if status is not None else None
    return payload


def _snapshot_payload(snapshot: Optional[BaselineSnapshot]) -> Optional[dict[str, Any]]:
    if snapshot is None:
        return None
    covered = [bucket for bucket in snapshot.hours if bucket.coverage > 0]
    return {
        "snapshot_id": snapshot.snapshot_id,
        "content_sha256": snapshot.content_sha256,
        "content_verified": snapshot.verify_digest(),
        "window": snapshot.window.model_dump(mode="json"),
        "hours": len(snapshot.hours),
        "covered_hours": len(covered),
        "cost_covered_hours": sum(1 for bucket in snapshot.hours if bucket.cost_coverage >= 0.5),
        "load_drivers": sorted(snapshot.load_series),
        "price_card": snapshot.price_card,
        "intensity_source": snapshot.intensity_source,
        "impact_model_version": snapshot.impact_model_version,
        "collector_versions": snapshot.collector_versions,
        "created_at": snapshot.created_at.isoformat(),
    }


def _cursor(change: ChangeEvent) -> str:
    return f"{ensure_utc(change.created_at).isoformat()}|{change.change_id}"


def _parse_cursor(cursor: Optional[str]) -> Optional[tuple[datetime, str]]:
    if not cursor:
        return None
    moment, separator, change_id = cursor.rpartition("|")
    if not separator or not change_id:
        raise HTTPException(status_code=400, detail="Invalid cursor.")
    try:
        return ensure_utc(datetime.fromisoformat(moment)), change_id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor.") from exc


@router.get("/changes")
async def list_changes(
    namespace: Optional[str] = Depends(validate_namespace),
    state: Optional[List[MeasurementState]] = Query(None, description="Filter by lifecycle state (repeatable)."),
    plan_id: Optional[str] = Query(
        None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description="Only the changes of this GitOps plan (the ledger link of a pull request).",
    ),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(None, max_length=200, description="next_cursor of the previous page."),
    repo: ChangeRepository = Depends(get_changes_repository),
) -> dict[str, Any]:
    """Applied changes, newest first."""
    before = _parse_cursor(cursor)
    try:
        rows = await repo.list_changes(
            namespace=namespace,
            states=[item.value for item in state] if state else None,
            limit=limit + 1,
            before=before,
            plan_id=plan_id,
        )
    except Exception as exc:
        logger.error("Could not list changes: %s", exc)
        raise HTTPException(status_code=503, detail="Changes are temporarily unavailable.") from exc
    page = rows[:limit]
    return {
        "items": [_change_payload(change, status) for change, status in page],
        "next_cursor": _cursor(page[-1][0]) if len(rows) > limit and page else None,
    }


@router.get("/changes/{change_id}")
async def get_change(
    change_id: str = _CHANGE_ID,
    repo: ChangeRepository = Depends(get_changes_repository),
) -> dict[str, Any]:
    """One change with its lifecycle state, baseline snapshot and events."""
    try:
        change = await repo.get_change(change_id)
        if change is None:
            raise HTTPException(status_code=404, detail="Change not found.")
        status = await repo.get_status(change_id)
        snapshot = await repo.get_snapshot_for_change(change_id)
        events = await repo.list_events(change_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Could not read change %s: %s", change_id, exc)
        raise HTTPException(status_code=503, detail="Changes are temporarily unavailable.") from exc
    return {
        **_change_payload(change, status),
        "baseline": _snapshot_payload(snapshot),
        "events": [event.model_dump(mode="json") for event in events],
    }


def savings_basis(measurement: Any) -> dict[str, Any]:
    """What a measurement's figures are (projected != allocated != realized), for labels.

    ``measured_allocated_annual`` is the measured saving of the change (allocated cost:
    what the workload stops being charged), ``projected_allocated_annual`` the allocated
    projection it is compared with (``projection_accuracy``), ``realization_factor`` the
    projections' share of allocated savings expected to leave the bill, and
    ``realized_billed_annual_expected`` = measured allocated x factor: an expectation,
    not a measurement (``realized_billed_measured`` is false until billed savings are
    measured). Measurements decided before these were recorded return ``None`` for
    the factor.
    """
    params = getattr(measurement, "counterfactual_params", None) or {}
    estimate = measurement.estimate("allocated_cost") if hasattr(measurement, "estimate") else None
    measured = estimate.annualized if estimate is not None else None
    factor = params.get("realization_factor")
    factor = float(factor) if isinstance(factor, (int, float)) and not isinstance(factor, bool) else None
    return {
        "measured_method": "measured_allocated",
        "projection_basis": params.get("projection_basis") or "billable_expected",
        "measured_allocated_annual": measured,
        "measured_allocated_claimed": bool(estimate is not None and estimate.claimed),
        "projected_allocated_annual": estimate.projected_annual if estimate is not None else None,
        "projection_accuracy": estimate.projection_accuracy if estimate is not None else None,
        "realization_factor": factor,
        "realized_billed_annual_expected": measured * factor if measured is not None and factor is not None else None,
        "realized_billed_measured": False,
    }


@router.get("/changes/{change_id}/measurements")
async def list_change_measurements(
    change_id: str = _CHANGE_ID,
    repo: ChangeRepository = Depends(get_changes_repository),
    measurements: SavingsMeasurementRepository = Depends(get_measurements_repository),
) -> dict[str, Any]:
    """Decided measurements of a change, oldest first, with their per-recommendation split."""
    try:
        change = await repo.get_change(change_id)
        if change is None:
            raise HTTPException(status_code=404, detail="Change not found.")
        status = await repo.get_status(change_id)
        decided = await repo.list_measurements(change_id)
        rows = await measurements.list_for_change(change_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Could not read measurements of change %s: %s", change_id, exc)
        raise HTTPException(status_code=503, detail="Measurements are temporarily unavailable.") from exc
    items = []
    for measurement in decided:
        payload = measurement.model_dump(mode="json")
        payload["savings_basis"] = savings_basis(measurement)
        payload["recommendations"] = [
            {
                "recommendation_id": row.recommendation_id,
                "measurement_id": row.measurement_id,
                "sequence": row.sequence,
                "outcome": row.outcome.value,
                "reasons": row.reasons,
                "estimates": [estimate.model_dump(mode="json") for estimate in row.estimates],
                "decided_at": row.decided_at.isoformat(),
            }
            for row in rows
            if row.group_id == measurement.measurement_id
        ]
        items.append(payload)
    return {
        "change_id": change_id,
        "state": status.state.value if status is not None else None,
        "items": items,
    }
