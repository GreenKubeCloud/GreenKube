"""Models for tracking optimization engine executions."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OptimizationRunStatus(str, Enum):
    """Terminal and in-progress states for an optimization run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OptimizationRun(BaseModel):
    """A durable record of one optimization execution."""

    id: Optional[int] = None
    status: OptimizationRunStatus = OptimizationRunStatus.RUNNING
    namespace: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    recommendation_count: int = Field(default=0, ge=0)
    analyzer_count: int = Field(default=0, ge=0)
    failed_analyzer_count: int = Field(default=0, ge=0)
    error: Optional[str] = None
