"""Helpers for turning potential recommendation savings into realized savings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)

#: Statuses of a recommendation whose change is recorded as applied (or being
#: measured). A regenerated recommendation for the same identity is matched
#: against them so the same change is never offered or recorded twice.
RECORDED_CHANGE_STATUSES: tuple[str, ...] = (
    RecommendationStatus.APPLIED.value,
    RecommendationStatus.VERIFYING.value,
    RecommendationStatus.VERIFIED.value,
    RecommendationStatus.ROLLBACK_REVIEW.value,
)

#: How long after a change's rollout a scan may still be built from usage data
#: recorded before it. Analyzers read requests from the hourly usage rollup,
#: whose latest bucket keeps the replaced request for up to an hour; the margin
#: covers scan intervals and late rollups.
ROLLUP_LAG_WINDOW = timedelta(hours=6)


def estimate_realized_savings(
    record: RecommendationRecord,
    request: ApplyRecommendationRequest,
) -> tuple[float | None, float | None]:
    """Estimate realized savings for an applied recommendation.

    For CPU and memory rightsizing recommendations, omitted savings are scaled
    proportionally when the actual applied request is more conservative than the
    original recommendation. For all other recommendation types, omitted values
    fall back to the original potential savings estimate.
    """

    share = _realized_share(record, request)
    return (
        _resolved_savings_value(request.carbon_saved_co2e_grams, record.potential_savings_co2e_grams, share),
        _resolved_savings_value(request.cost_saved, record.potential_savings_cost, share),
    )


def refresh_applied_recommendation(
    applied_record: RecommendationRecord,
    observed_record: RecommendationRecord,
    observed_at: datetime | None = None,
) -> RecommendationRecord:
    """Refresh an applied recommendation when a scan regenerates the same identity.

    For rightsizing, only ``updated_at`` changes: ``current_*`` stays the
    immutable pre-change baseline, and ``actual_*`` stays the live value that
    apply detection (or the operator) recorded once the change reached the
    cluster, together with the savings derived from it.

    A scan is not a live observation: analyzers read requests from the hourly
    usage rollup, whose latest bucket still carries the pre-change request for
    up to an hour after a rollout. Copying the regenerated ``current_*`` into
    ``actual_*`` reverted the recorded value to the one that was replaced, so
    merged changes showed identical before and after values and their savings
    dropped to zero. Drift after a change is the drift monitor's job (C8),
    measured from the cluster, not from a regenerated recommendation.
    """
    del observed_record  # A regenerated recommendation is advice, not a live observation.
    updates: dict = {"updated_at": observed_at or datetime.now(timezone.utc)}
    if applied_record.type in _KEEP_SAVINGS_ON_REFRESH:
        # Rightsizing: the regenerated advice never rewrites the applied change.
        # Orphaned resources: the identity is the resource itself, so a regenerated
        # observation means its deletion is pending, not that the issue reappeared.
        return applied_record.model_copy(update=updates)

    # Any other applied recommendation generated again means the issue is back.
    updates.update(carbon_saved_co2e_grams=0.0, cost_saved=0.0)
    return applied_record.model_copy(update=updates)


def absorbs_regenerated_recommendation(
    recorded: RecommendationRecord,
    regenerated: RecommendationRecord,
    now: datetime | None = None,
) -> bool:
    """True when a regenerated rightsizing only restates a change already recorded.

    ``recorded`` is a recommendation of the same identity in a status of
    :data:`RECORDED_CHANGE_STATUSES` (other than ``applied``, which always
    absorbs its regeneration through :func:`refresh_applied_recommendation`).
    Within :data:`ROLLUP_LAG_WINDOW` after the change, a scan built from the
    lagging rollup regenerates the pre-change advice (same baseline, or the
    proposal that is already live). Inserting it as a new active
    recommendation offered the change again, and apply detection could record
    it as a second applied change with its own measurement and double-counted
    savings. Outside that window a regenerated recommendation is new advice
    (for example after the request drifted back) and is stored normally.
    """
    if recorded.type not in _RIGHTSIZING_TYPES or regenerated.type != recorded.type:
        return False
    reference = recorded.applied_at or recorded.updated_at or recorded.created_at
    if reference is None:
        return False
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    if (now or datetime.now(timezone.utc)) - reference > ROLLUP_LAG_WINDOW:
        return False
    if recorded.type == RecommendationType.RIGHTSIZING_CPU:
        baseline = recorded.current_cpu_request_millicores
        landed = (recorded.recommended_cpu_request_millicores, recorded.actual_cpu_request_millicores)
        regenerated_current = regenerated.current_cpu_request_millicores
        regenerated_proposal = regenerated.recommended_cpu_request_millicores
    else:
        baseline = recorded.current_memory_request_bytes
        landed = (recorded.recommended_memory_request_bytes, recorded.actual_memory_request_bytes)
        regenerated_current = regenerated.current_memory_request_bytes
        regenerated_proposal = regenerated.recommended_memory_request_bytes
    if baseline is not None and regenerated_current == baseline:
        return True
    return regenerated_proposal is not None and regenerated_proposal in landed


_RIGHTSIZING_TYPES = frozenset({RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY})

_KEEP_SAVINGS_ON_REFRESH = frozenset(
    {
        RecommendationType.RIGHTSIZING_CPU,
        RecommendationType.RIGHTSIZING_MEMORY,
        RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        RecommendationType.ORPHANED_LOAD_BALANCER,
    }
)


def _resolved_savings_value(
    explicit_value: float | None,
    potential_value: float | None,
    share: float | None,
) -> float | None:
    if explicit_value is not None:
        return explicit_value
    if potential_value is None:
        return None
    if share is None:
        return potential_value
    return potential_value * share


def _realized_share(
    record: RecommendationRecord,
    request: ApplyRecommendationRequest,
) -> float | None:
    if record.type == RecommendationType.RIGHTSIZING_CPU:
        return _scaled_share(
            current_value=record.current_cpu_request_millicores,
            recommended_value=record.recommended_cpu_request_millicores,
            actual_value=request.actual_cpu_request_millicores,
        )

    if record.type == RecommendationType.RIGHTSIZING_MEMORY:
        return _scaled_share(
            current_value=record.current_memory_request_bytes,
            recommended_value=record.recommended_memory_request_bytes,
            actual_value=request.actual_memory_request_bytes,
        )

    return None


def _scaled_share(
    *,
    current_value: int | None,
    recommended_value: int | None,
    actual_value: int | None,
) -> float | None:
    if current_value is None or recommended_value is None or actual_value is None:
        return None

    recommended_delta = current_value - recommended_value
    if recommended_delta <= 0:
        return None

    actual_delta = current_value - actual_value
    raw_share = actual_delta / recommended_delta
    return max(0.0, min(raw_share, 1.0))
