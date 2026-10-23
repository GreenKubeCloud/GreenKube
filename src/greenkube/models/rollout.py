"""Durable state for progressive recommendation rollouts.

The rollout state is deliberately independent from a provider (Argo CD, Flux,
or a Git provider).  Providers report observations and execute a desired
percentage; the controller owns the state machine and its safety invariants.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class RolloutPhase(str, Enum):
    PENDING = "pending"
    PROGRESSING = "progressing"
    PAUSED = "paused"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class RolloutHealth(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    INCONCLUSIVE = "inconclusive"
    UNHEALTHY = "unhealthy"


class RolloutStep(BaseModel):
    """One monotonically increasing traffic or replica percentage."""

    percentage: int = Field(ge=1, le=100)
    observation_seconds: int = Field(default=0, ge=0)


class ProgressiveRollout(BaseModel):
    """A versioned, serializable progressive rollout state."""

    id: Optional[int] = None
    recommendation_id: int
    steps: tuple[RolloutStep, ...] = Field(min_length=1)
    current_step: int = Field(default=-1, ge=-1)
    phase: RolloutPhase = RolloutPhase.PENDING
    health: RolloutHealth = RolloutHealth.UNKNOWN
    generation: int = Field(default=0, ge=0)
    started_at: Optional[datetime] = None
    step_started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_error: Optional[str] = None

    @model_validator(mode="after")
    def validate_steps(self) -> "ProgressiveRollout":
        percentages = [step.percentage for step in self.steps]
        if percentages != sorted(set(percentages)):
            raise ValueError("rollout step percentages must be strictly increasing")
        return self

    @property
    def target_percentage(self) -> int:
        return self.steps[self.current_step].percentage if self.current_step >= 0 else 0

    @property
    def is_terminal(self) -> bool:
        return self.phase in {
            RolloutPhase.COMPLETED,
            RolloutPhase.ROLLED_BACK,
            RolloutPhase.FAILED,
        }

    def now(self) -> datetime:
        return datetime.now(timezone.utc)
