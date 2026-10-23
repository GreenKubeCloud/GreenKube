from datetime import datetime, timedelta, timezone

from greenkube.automation.rollout import (
    RolloutAction,
    RolloutObservation,
    RolloutReconciler,
)
from greenkube.models.rollout import (
    ProgressiveRollout,
    RolloutHealth,
    RolloutPhase,
    RolloutStep,
)


def rollout() -> ProgressiveRollout:
    return ProgressiveRollout(
        recommendation_id=7,
        steps=(
            RolloutStep(percentage=10, observation_seconds=60),
            RolloutStep(percentage=50, observation_seconds=60),
            RolloutStep(percentage=100),
        ),
    )


def test_reconcile_advances_only_after_healthy_dwell_window():
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reconciler = RolloutReconciler(now=lambda: clock)
    first = reconciler.reconcile(rollout(), RolloutObservation(health=RolloutHealth.HEALTHY, observed_at=clock))
    assert first.action == RolloutAction.APPLY
    assert first.desired_percentage == 10

    waiting = reconciler.reconcile(
        first.state,
        RolloutObservation(
            observed_percentage=10,
            health=RolloutHealth.HEALTHY,
            observed_at=clock + timedelta(seconds=59),
        ),
    )
    assert waiting.action == RolloutAction.NOOP
    assert waiting.state.current_step == 0

    next_step = reconciler.reconcile(
        waiting.state,
        RolloutObservation(
            observed_percentage=10,
            health=RolloutHealth.HEALTHY,
            observed_at=clock + timedelta(seconds=60),
        ),
    )
    assert next_step.action == RolloutAction.APPLY
    assert next_step.desired_percentage == 50


def test_unhealthy_observation_requests_rollback_without_advancing():
    current = rollout().model_copy(
        update={
            "current_step": 1,
            "phase": RolloutPhase.PROGRESSING,
            "step_started_at": datetime.now(timezone.utc),
        }
    )
    result = RolloutReconciler().reconcile(
        current,
        RolloutObservation(observed_percentage=50, health=RolloutHealth.UNHEALTHY),
    )
    assert result.action == RolloutAction.ROLLBACK
    assert result.state.phase == RolloutPhase.ROLLING_BACK
    assert result.state.current_step == 1


def test_final_healthy_step_completes_and_repeated_reconcile_is_noop():
    current = rollout().model_copy(
        update={
            "current_step": 2,
            "phase": RolloutPhase.PROGRESSING,
            "step_started_at": datetime.now(timezone.utc),
        }
    )
    reconciler = RolloutReconciler()
    result = reconciler.reconcile(
        current,
        RolloutObservation(observed_percentage=100, health=RolloutHealth.HEALTHY),
    )
    assert result.action == RolloutAction.COMPLETE
    assert result.state.phase == RolloutPhase.COMPLETED
    repeated = reconciler.reconcile(
        result.state,
        RolloutObservation(observed_percentage=100, health=RolloutHealth.HEALTHY),
    )
    assert repeated.action == RolloutAction.NOOP
    assert repeated.state.generation == result.state.generation
