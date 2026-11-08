"""Models for tracking optimization engine executions.

An ``optimization_runs`` row is both the request queue entry and the execution
record of one recommendation scan:

* ``queued``    - requested (API ``POST /optimization/runs``, deprecated GET
  ``refresh=true`` or the controller schedule) and waiting for the controller;
* ``running``   - claimed by exactly one controller process;
* ``succeeded`` / ``failed`` - terminal, with per-source health and the diff
  against the previously active recommendation set.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class OptimizationRunStatus(str, Enum):
    """Queue, in-progress and terminal states for an optimization run."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OptimizationRunTrigger(str, Enum):
    """Why a run was requested."""

    #: Controller schedule (``RECOMMENDATION_SCAN_INTERVAL``).
    SCHEDULED = "scheduled"
    #: Explicit ``POST /api/v1/optimization/runs``.
    MANUAL = "manual"
    #: Deprecated ``refresh=true`` on a recommendation GET endpoint.
    REFRESH = "refresh"
    #: Direct engine call outside the queue (tests, embedding callers).
    DIRECT = "direct"
    #: Rows written before migration 0045.
    LEGACY = "legacy"


class SourceRunStatus(str, Enum):
    """Outcome of one recommendation source within a run."""

    #: The source ran and reported a complete result.
    SUCCEEDED = "succeeded"
    #: The source ran but reported a partial result (e.g. a collector page failed).
    INCOMPLETE = "incomplete"
    #: The source raised or timed out.
    FAILED = "failed"
    #: The source's availability probe returned False (e.g. CRD not installed).
    UNAVAILABLE = "unavailable"


class SourceRunOutcome(BaseModel):
    """Per-source health for one optimization run (E8/E10)."""

    source: str
    status: SourceRunStatus
    complete: bool = Field(
        False,
        description=(
            "True only when the source finished and its result is authoritative. Recommendations "
            "supported by a source that is not complete are never marked stale by this run."
        ),
    )
    recommendation_count: int = Field(default=0, ge=0)
    duration_seconds: Optional[float] = Field(default=None, ge=0)
    error: Optional[str] = None


class OptimizationRunDiff(BaseModel):
    """Difference between the active set before a run and the run's output (E10).

    Identities are recommendation fingerprints. Lists are capped at
    :data:`DIFF_SAMPLE_LIMIT` entries each; counts are always exact.
    """

    new: List[str] = Field(default_factory=list)
    changed: List[str] = Field(default_factory=list)
    resolved: List[str] = Field(default_factory=list)
    truncated: bool = False


#: Maximum fingerprints kept per diff list on a persisted run.
DIFF_SAMPLE_LIMIT = 100


class OptimizationRun(BaseModel):
    """A durable record of one optimization request and execution."""

    id: Optional[int] = None
    status: OptimizationRunStatus = OptimizationRunStatus.RUNNING
    namespace: Optional[str] = None
    trigger: OptimizationRunTrigger = OptimizationRunTrigger.DIRECT
    requested_by: Optional[str] = None
    requested_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    claimed_by: Optional[str] = None
    recommendation_count: int = Field(default=0, ge=0)
    analyzer_count: int = Field(default=0, ge=0)
    failed_analyzer_count: int = Field(default=0, ge=0)
    new_count: Optional[int] = Field(default=None, ge=0)
    changed_count: Optional[int] = Field(default=None, ge=0)
    resolved_count: Optional[int] = Field(default=None, ge=0)
    unchanged_count: Optional[int] = Field(default=None, ge=0)
    source_outcomes: List[SourceRunOutcome] = Field(default_factory=list)
    diff: Optional[OptimizationRunDiff] = None
    error: Optional[str] = None


class OptimizationRunCompletion(BaseModel):
    """Terminal state written when a run finishes."""

    status: OptimizationRunStatus
    completed_at: datetime
    recommendation_count: int = Field(default=0, ge=0)
    analyzer_count: int = Field(default=0, ge=0)
    failed_analyzer_count: int = Field(default=0, ge=0)
    new_count: Optional[int] = Field(default=None, ge=0)
    changed_count: Optional[int] = Field(default=None, ge=0)
    resolved_count: Optional[int] = Field(default=None, ge=0)
    unchanged_count: Optional[int] = Field(default=None, ge=0)
    source_outcomes: List[SourceRunOutcome] = Field(default_factory=list)
    diff: Optional[OptimizationRunDiff] = None
    error: Optional[str] = None


class OptimizationRunAccepted(BaseModel):
    """Response of ``POST /api/v1/optimization/runs`` (HTTP 202)."""

    run_id: int
    status: OptimizationRunStatus


class OptimizationRunRequest(BaseModel):
    """Optional body of ``POST /api/v1/optimization/runs``."""

    namespace: Optional[str] = Field(
        default=None,
        description="Restrict the scan (and its reconciliation) to one namespace. Omit for a cluster-wide scan.",
    )


class OptimizationRunResponse(BaseModel):
    """Public API view of one optimization run."""

    run_id: int
    status: OptimizationRunStatus
    trigger: OptimizationRunTrigger
    namespace: Optional[str] = None
    requested_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    recommendation_count: int = 0
    source_count: int = 0
    failed_source_count: int = 0
    new_count: Optional[int] = None
    changed_count: Optional[int] = None
    resolved_count: Optional[int] = None
    unchanged_count: Optional[int] = None
    sources: List[SourceRunOutcome] = Field(default_factory=list)
    diff: Optional[OptimizationRunDiff] = None
    error: Optional[str] = None

    @classmethod
    def from_run(cls, run: OptimizationRun) -> "OptimizationRunResponse":
        if run.id is None:
            raise ValueError("Only persisted optimization runs can be serialized.")
        return cls(
            run_id=run.id,
            status=run.status,
            trigger=run.trigger,
            namespace=run.namespace,
            requested_at=run.requested_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            recommendation_count=run.recommendation_count,
            source_count=run.analyzer_count,
            failed_source_count=run.failed_analyzer_count,
            new_count=run.new_count,
            changed_count=run.changed_count,
            resolved_count=run.resolved_count,
            unchanged_count=run.unchanged_count,
            sources=list(run.source_outcomes),
            diff=run.diff,
            error=run.error,
        )
