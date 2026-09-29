"""Deterministic reconciliation for progressive rollouts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from greenkube.models.rollout import (
    ProgressiveRollout,
    RolloutHealth,
    RolloutPhase,
)


class RolloutAction(str, Enum):
    NOOP = "noop"
    APPLY = "apply"
    PAUSE = "pause"
    ROLLBACK = "rollback"
    COMPLETE = "complete"


@dataclass(frozen=True)
class RolloutObservation:
    """The provider and health gate view at one reconciliation point."""

    observed_percentage: int = 0
    health: RolloutHealth = RolloutHealth.UNKNOWN
    observed_at: Optional[datetime] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class RolloutReconcileResult:
    state: ProgressiveRollout
    action: RolloutAction
    desired_percentage: int
    reason: str


class RolloutReconciler:
    """Advances a rollout only after its health gate and dwell time pass.

    Reconciliation is idempotent: observing the same provider state twice does
    not increment the step or generation.  Unknown and inconclusive health
    never advance a rollout; unhealthy health always requests rollback.
    """

    def __init__(self, *, now=None):
        self._now = now or (lambda: datetime.now(timezone.utc))

    def reconcile(
        self,
        rollout: ProgressiveRollout,
        observation: RolloutObservation,
    ) -> RolloutReconcileResult:
        if rollout.is_terminal:
            return self._result(rollout, RolloutAction.NOOP, "rollout is terminal")

        if observation.error:
            return self._result(
                rollout.model_copy(
                    update={
                        "phase": RolloutPhase.PAUSED,
                        "last_error": observation.error,
                        "health": observation.health,
                    }
                ),
                RolloutAction.PAUSE,
                "provider observation failed",
            )

        if observation.health == RolloutHealth.UNHEALTHY:
            state = rollout.model_copy(
                update={
                    "phase": RolloutPhase.ROLLING_BACK,
                    "health": observation.health,
                    "last_error": "health gate failed",
                }
            )
            return self._result(state, RolloutAction.ROLLBACK, "health gate failed")

        state = rollout.model_copy(update={"health": observation.health})
        if state.current_step < 0:
            return self._advance(state, observation)

        step = state.steps[state.current_step]
        started = state.step_started_at
        current_time = observation.observed_at or self._now()
        dwell_complete = (
            step.observation_seconds == 0
            or started is not None
            and current_time >= started + timedelta(seconds=step.observation_seconds)
        )
        if (
            observation.health != RolloutHealth.HEALTHY
            or not dwell_complete
            or observation.observed_percentage < step.percentage
        ):
            return self._result(state, RolloutAction.NOOP, "waiting for a healthy observation window")

        if state.current_step == len(state.steps) - 1:
            return self._result(
                state.model_copy(
                    update={
                        "phase": RolloutPhase.COMPLETED,
                        "completed_at": current_time,
                        "generation": state.generation + 1,
                    }
                ),
                RolloutAction.COMPLETE,
                "final rollout step is healthy",
            )
        return self._advance(state, observation)

    def _advance(self, rollout: ProgressiveRollout, observation: RolloutObservation) -> RolloutReconcileResult:
        next_step = rollout.current_step + 1
        target = rollout.steps[next_step].percentage
        now = observation.observed_at or self._now()
        state = rollout.model_copy(
            update={
                "current_step": next_step,
                "phase": RolloutPhase.PROGRESSING,
                "health": observation.health,
                "started_at": rollout.started_at or now,
                "step_started_at": now,
                "generation": rollout.generation + 1,
                "last_error": None,
            }
        )
        return self._result(state, RolloutAction.APPLY, f"advance to {target}%")

    @staticmethod
    def _result(state: ProgressiveRollout, action: RolloutAction, reason: str) -> RolloutReconcileResult:
        return RolloutReconcileResult(
            state=state,
            action=action,
            desired_percentage=state.target_percentage,
            reason=reason,
        )
