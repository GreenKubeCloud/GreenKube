"""Helpers for turning potential recommendation savings into realized savings."""

from __future__ import annotations

from datetime import datetime, timezone

from greenkube.models.metrics import ApplyRecommendationRequest, RecommendationRecord, RecommendationType


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
    """Refresh an applied recommendation from a newer generated observation.

    The original recommendation remains the baseline for realized savings, while
    live resource observations update the actual value currently in effect. This
    lets future savings attribution decrease when a rightsized request drifts
    back up, without rewriting savings already written to the ledger.
    """

    updates: dict = {"updated_at": observed_at or datetime.now(timezone.utc)}

    if applied_record.type == RecommendationType.RIGHTSIZING_CPU:
        actual_cpu = observed_record.current_cpu_request_millicores
        if actual_cpu is None:
            return applied_record.model_copy(update=updates)

        carbon_saved, cost_saved = estimate_realized_savings(
            applied_record,
            ApplyRecommendationRequest(actual_cpu_request_millicores=actual_cpu),
        )
        updates.update(
            actual_cpu_request_millicores=actual_cpu,
            carbon_saved_co2e_grams=carbon_saved,
            cost_saved=cost_saved,
        )
        return applied_record.model_copy(update=updates)

    if applied_record.type == RecommendationType.RIGHTSIZING_MEMORY:
        actual_memory = observed_record.current_memory_request_bytes
        if actual_memory is None:
            return applied_record.model_copy(update=updates)

        carbon_saved, cost_saved = estimate_realized_savings(
            applied_record,
            ApplyRecommendationRequest(actual_memory_request_bytes=actual_memory),
        )
        updates.update(
            actual_memory_request_bytes=actual_memory,
            carbon_saved_co2e_grams=carbon_saved,
            cost_saved=cost_saved,
        )
        return applied_record.model_copy(update=updates)

    updates.update(carbon_saved_co2e_grams=0.0, cost_saved=0.0)
    return applied_record.model_copy(update=updates)


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
