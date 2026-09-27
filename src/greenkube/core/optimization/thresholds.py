# src/greenkube/core/optimization/thresholds.py
"""Recommendation value clamping shared across analyzers and the engine."""

from typing import TYPE_CHECKING

from greenkube.models.metrics import Recommendation

if TYPE_CHECKING:
    from greenkube.core.config import Config


def apply_minimum_thresholds(rec: Recommendation, config: "Config") -> Recommendation:
    """Clamps recommended resource values to configured minimum thresholds.

    Ensures that no recommendation asks for an impractically small resource
    request (e.g. 3m CPU). The description is updated to mention the floor
    when clamping occurs.

    Args:
        rec: The recommendation to validate and possibly clamp.
        config: The active configuration providing minimum thresholds.

    Returns:
        The recommendation with values floored to configured minimums.
    """
    updates: dict = {}

    if (
        rec.recommended_cpu_request_millicores is not None
        and rec.recommended_cpu_request_millicores < config.RECOMMENDATION_MIN_CPU_MILLICORES
    ):
        updates["recommended_cpu_request_millicores"] = config.RECOMMENDATION_MIN_CPU_MILLICORES
        updates["description"] = (
            rec.description + f" (Floored to minimum: {config.RECOMMENDATION_MIN_CPU_MILLICORES}m CPU.)"
        )
        updates["reason"] = rec.reason + (
            f" Recommended value was below the minimum of {config.RECOMMENDATION_MIN_CPU_MILLICORES}m; "
            "floored to avoid impractically small requests."
        )

    if (
        rec.recommended_memory_request_bytes is not None
        and rec.recommended_memory_request_bytes < config.RECOMMENDATION_MIN_MEMORY_BYTES
    ):
        mb = config.RECOMMENDATION_MIN_MEMORY_BYTES // (1024 * 1024)
        updates["recommended_memory_request_bytes"] = config.RECOMMENDATION_MIN_MEMORY_BYTES
        desc = updates.get("description", rec.description)
        updates["description"] = desc + f" (Floored to minimum: {mb}MiB memory.)"
        reason = updates.get("reason", rec.reason)
        updates["reason"] = reason + (
            f" Recommended memory was below the minimum of {mb}MiB; floored to avoid impractically small requests."
        )

    if updates:
        rec = rec.model_copy(update=updates)

    return rec
