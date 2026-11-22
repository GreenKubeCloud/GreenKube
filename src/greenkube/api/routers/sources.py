"""Recommendation sources: registry, health and run history (roadmap E5).

Sources run only in the controller's scans; the API reads what they recorded.

Endpoints:
  GET /sources                        - Every source: metadata, enablement, status, health, last run, counts
  GET /sources/{source_id}/runs       - A source's runs, newest first (?limit=)
  GET /sources/{source_id}/signals    - A source's signals, most recently seen first (?limit=, ?target_key=)

``GET /sources/track-record`` (realization rates per source family) lives in
the recommendations router.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from greenkube.signals.model import SourceSignal
from greenkube.signals.registry import ConfiguredSource, RegistryProblem, SourceRegistry
from greenkube.signals.runtime import SourceRunRecord, SourceRunStatus
from greenkube.signals.sdk import Diagnostic, EnabledMode, SourceHealthStatus, SourceLimits
from greenkube.storage.base_signal_repository import MAX_LIST_LIMIT, SignalRepository, SourceRunSummary

logger = logging.getLogger(__name__)

router = APIRouter()

_SOURCE_ID = Path(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class SourceRunResponse(BaseModel):
    run_id: Optional[int]
    optimization_run_id: Optional[int] = None
    source_id: str
    source_version: Optional[str] = None
    namespace: Optional[str] = None
    status: SourceRunStatus
    complete: bool
    health_status: Optional[SourceHealthStatus] = None
    health_reason: Optional[str] = None
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    signal_count: int
    recommendation_count: int
    consecutive_failures: int
    error_code: Optional[str] = None
    error: Optional[str] = None
    diagnostics: List[Diagnostic] = Field(default_factory=list)

    @classmethod
    def from_record(cls, record: SourceRunRecord) -> "SourceRunResponse":
        data = record.model_dump(exclude={"id", "last_collected_at"})
        return cls(run_id=record.id, **data)


class SourceHealthResponse(BaseModel):
    status: Optional[SourceHealthStatus] = None
    reason: Optional[str] = None


class SourceResponse(BaseModel):
    id: str
    display_name: str
    description: str = ""
    tier: str
    #: ``builtin``, ``plugin``, or ``unregistered`` (it ran before but is no longer installed).
    origin: str
    entry_point: Optional[str] = None
    distribution: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    version: Optional[str] = None
    #: v1 ``Recommendation.source`` its recommendations carry; None when it only emits signals.
    legacy_source: Optional[str] = None
    enabled: Optional[EnabledMode] = None
    #: ``healthy``, ``degraded``, ``failing``, ``unavailable``, ``disabled``, ``misconfigured``,
    #: ``pending`` (never ran yet) or ``unregistered``.
    status: str
    status_reason: Optional[str] = None
    health: Optional[SourceHealthResponse] = None
    last_run: Optional[SourceRunResponse] = None
    last_success_at: Optional[datetime] = None
    consecutive_failures: int = 0
    signal_count: int = 0
    recommendation_count: int = 0
    limits: Optional[Dict[str, Any]] = None


class RegistryProblemResponse(BaseModel):
    code: str
    message: str
    source_id: Optional[str] = None
    entry_point: Optional[str] = None


class SourceListResponse(BaseModel):
    sources: List[SourceResponse] = Field(default_factory=list)
    problems: List[RegistryProblemResponse] = Field(default_factory=list)


class StoredSignalResponse(BaseModel):
    signal: SourceSignal
    first_seen_at: datetime
    last_seen_at: datetime
    last_run_id: Optional[int] = None
    recommendation_fingerprint: Optional[str] = None


async def get_signal_repository() -> SignalRepository:
    from greenkube.storage.base_signal_repository import create_signal_repository

    return create_signal_repository()


_catalog_cache: Dict[Tuple[bool, str], Tuple[SourceRegistry, List[ConfiguredSource]]] = {}


def get_source_catalog() -> Tuple[SourceRegistry, List[ConfiguredSource]]:
    """The registry and resolved configuration, loaded once per configuration (plugins import once)."""
    from greenkube.core.config import get_config
    from greenkube.core.optimization.registry import build_source_registry

    config = get_config()
    key = (bool(getattr(config, "SOURCES_PLUGINS_ENABLED", True)), str(getattr(config, "SOURCES_CONFIG", "")))
    cached = _catalog_cache.get(key)
    if cached is None:
        cached = build_source_registry(config)
        _catalog_cache.clear()
        _catalog_cache[key] = cached
    return cached


def _status(item: Optional[ConfiguredSource], summary: Optional[SourceRunSummary]) -> Tuple[str, Optional[str]]:
    if item is None:
        return "unregistered", "this source ran before but is no longer installed or loaded"
    if item.error is not None:
        return "misconfigured", item.error
    if item.enabled == EnabledMode.FALSE:
        return "disabled", None
    if summary is None:
        return "pending", "no scan has run this source yet"
    latest = summary.latest
    if latest.status == SourceRunStatus.SUCCEEDED:
        return (
            ("degraded", latest.health_reason)
            if latest.health_status == SourceHealthStatus.DEGRADED
            else (
                "healthy",
                None,
            )
        )
    if latest.status in (SourceRunStatus.INCOMPLETE, SourceRunStatus.SKIPPED):
        return "degraded", latest.error or latest.error_code
    if latest.status == SourceRunStatus.FAILED:
        return "failing", latest.error or latest.error_code
    reason = latest.health_reason or "prerequisites not found"
    if item.enabled == EnabledMode.TRUE:
        return "failing", f"enabled explicitly but unavailable: {reason}"
    return "unavailable", reason


def _limits(limits: SourceLimits) -> Dict[str, Any]:
    return limits.model_dump()


def _response(item: Optional[ConfiguredSource], source_id: str, summary: Optional[SourceRunSummary]) -> SourceResponse:
    status, reason = _status(item, summary)
    latest = summary.latest if summary is not None else None
    common: Dict[str, Any] = {
        "status": status,
        "status_reason": reason,
        "last_run": SourceRunResponse.from_record(latest) if latest is not None else None,
        "last_success_at": summary.last_success_at if summary is not None else None,
        "consecutive_failures": latest.consecutive_failures if latest is not None else 0,
        "signal_count": latest.signal_count if latest is not None else 0,
        "recommendation_count": latest.recommendation_count if latest is not None else 0,
        "health": SourceHealthResponse(status=latest.health_status, reason=latest.health_reason)
        if latest is not None and latest.health_status is not None
        else None,
    }
    if item is None:
        return SourceResponse(
            id=source_id,
            display_name=source_id,
            tier="custom",
            origin="unregistered",
            version=latest.source_version if latest is not None else None,
            **common,
        )
    descriptor = item.descriptor
    cls = descriptor.source_class
    return SourceResponse(
        id=descriptor.id,
        display_name=cls.display_name,
        description=cls.description,
        tier=cls.tier.value,
        origin=descriptor.origin,
        entry_point=descriptor.entry_point,
        distribution=descriptor.distribution,
        capabilities=sorted(capability.value for capability in cls.capabilities),
        version=cls.version,
        legacy_source=cls.legacy_source,
        enabled=item.enabled,
        limits=_limits(cls.limits),
        **common,
    )


async def _summaries(repo: SignalRepository) -> Dict[str, SourceRunSummary]:
    try:
        return await repo.latest_runs()
    except Exception as exc:
        logger.warning("Could not read source runs (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Source run history is unavailable.") from exc


def _problem(problem: RegistryProblem) -> RegistryProblemResponse:
    return RegistryProblemResponse(
        code=problem.code,
        message=problem.message,
        source_id=problem.source_id,
        entry_point=problem.entry_point,
    )


@router.get("/sources", response_model=SourceListResponse)
async def list_sources(
    repo: SignalRepository = Depends(get_signal_repository),
    catalog: Tuple[SourceRegistry, List[ConfiguredSource]] = Depends(get_source_catalog),
) -> SourceListResponse:
    """Every registered source with its enablement, status, health, last run and counts.

    Built-in sources come first in execution order, then plugins; sources that
    ran before but are no longer registered are listed last as ``unregistered``.
    Registry problems (a plugin that failed to load, invalid settings, settings
    for an unknown source) are listed under ``problems``.
    """
    registry, configured = catalog
    summaries = await _summaries(repo)
    sources = [_response(item, item.id, summaries.get(item.id)) for item in configured]
    known = {item.id for item in configured}
    sources.extend(
        _response(None, source_id, summary)
        for source_id, summary in sorted(summaries.items())
        if source_id not in known
    )
    return SourceListResponse(sources=sources, problems=[_problem(problem) for problem in registry.problems])


def _ensure_known(source_id: str, configured: List[ConfiguredSource], has_rows: bool) -> None:
    if has_rows or any(item.id == source_id for item in configured):
        return
    raise HTTPException(status_code=404, detail=f"Unknown source '{source_id}'.")


@router.get(
    "/sources/{source_id}/runs",
    response_model=List[SourceRunResponse],
    responses={404: {"description": "Unknown source."}},
)
async def list_source_runs(
    source_id: str = _SOURCE_ID,
    limit: int = Query(20, ge=1, le=MAX_LIST_LIMIT, description="Number of runs to return, newest first."),
    repo: SignalRepository = Depends(get_signal_repository),
    catalog: Tuple[SourceRegistry, List[ConfiguredSource]] = Depends(get_source_catalog),
) -> List[SourceRunResponse]:
    """A source's runs (status, health, counts, error code, diagnostics), newest first."""
    _registry, configured = catalog
    runs = await repo.list_runs(source_id, limit=limit)
    _ensure_known(source_id, configured, bool(runs))
    return [SourceRunResponse.from_record(run) for run in runs]


@router.get(
    "/sources/{source_id}/signals",
    response_model=List[StoredSignalResponse],
    responses={404: {"description": "Unknown source."}},
)
async def list_source_signals(
    source_id: str = _SOURCE_ID,
    limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT, description="Number of signals, most recently seen first."),
    target_key: Optional[str] = Query(None, max_length=512, description="Only signals about this target key."),
    repo: SignalRepository = Depends(get_signal_repository),
    catalog: Tuple[SourceRegistry, List[ConfiguredSource]] = Depends(get_source_catalog),
) -> List[StoredSignalResponse]:
    """A source's v2 signals with when they were first and last seen (kept 30 days after the last sighting)."""
    _registry, configured = catalog
    stored = await repo.list_signals(source_id, limit=limit, target_key=target_key)
    _ensure_known(source_id, configured, bool(stored))
    return [
        StoredSignalResponse(
            signal=item.signal,
            first_seen_at=item.first_seen_at,
            last_seen_at=item.last_seen_at,
            last_run_id=item.last_run_id,
            recommendation_fingerprint=item.recommendation_fingerprint,
        )
        for item in stored
    ]
