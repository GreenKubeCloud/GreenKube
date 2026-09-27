# src/greenkube/core/optimization/verifier.py
"""Post-apply verification of recommendation outcomes (Phase 3).

Verification separates "apply succeeded" from "recommendation succeeded". Once
the observation window elapses, the verifier compares the measured cost and
carbon signals against the projected reduction and checks workload health
(restarts, utilization headroom). Outcomes: ``verified``,
``rollback_review``, ``inconclusive`` or ``applied`` with
``savings_realized=false``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional, Sequence

from greenkube.core.optimization.statistics import percentile
from greenkube.models.metrics import RecommendationRecord, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle
    from greenkube.storage.base_repository import CombinedMetricsRepository

logger = logging.getLogger(__name__)

_SECONDS_PER_YEAR = 365.25 * 24 * 3600

#: Statuses that are candidates for verification.
VERIFIABLE_STATUSES = ["applied", "verifying"]


@dataclass
class VerificationOutcome:
    """Computed verification result before it is persisted."""

    outcome: str
    reasons: List[str] = field(default_factory=list)
    measured_co2e_saved_grams: float = 0.0
    measured_cost_saved: float = 0.0
    sample_count: int = 0
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


@dataclass
class _Measured:
    cost_per_hour_after: Optional[float] = None
    co2e_grams_per_hour_after: Optional[float] = None
    cpu_p95: Optional[float] = None
    memory_p95: Optional[float] = None
    restart_count: int = 0
    sample_count: int = 0


class RecommendationVerifier:
    """Evaluates applied recommendations once their observation window elapses."""

    def __init__(
        self,
        lifecycle: "RecommendationLifecycle",
        combined_repo: "CombinedMetricsRepository",
        config: Optional["Config"] = None,
    ):
        from greenkube.core.config import get_config

        self.lifecycle = lifecycle
        self.combined_repo = combined_repo
        self.config = config if config is not None else get_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify_due(self, namespace: Optional[str] = None) -> List[RecommendationRecord]:
        """Verifies every applied recommendation whose window has elapsed."""
        records = await self.lifecycle.repo.get_recommendations_by_statuses(VERIFIABLE_STATUSES, namespace=namespace)
        verified: List[RecommendationRecord] = []
        for record in records:
            try:
                updated = await self._verify_one(record)
            except Exception as exc:
                logger.warning("Verification failed for recommendation %s: %s", record.id, exc)
                continue
            if updated is not None:
                verified.append(updated)
        return verified

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _verify_one(self, record: RecommendationRecord) -> Optional[RecommendationRecord]:
        if record.applied_at is None or record.id is None:
            return None

        window_hours = float(getattr(self.config, "VERIFICATION_WINDOW_HOURS", 72))
        now = datetime.now(timezone.utc)
        window_start = record.applied_at
        window_end = record.applied_at + timedelta(hours=window_hours)
        # Allow a small grace period so the collection cycle has time to catch up.
        extension = 0.1
        if now < window_end + timedelta(hours=max(window_hours * extension, 0.05)):
            return None

        measured = await self._measure(record, window_start, now)
        min_samples = int(getattr(self.config, "VERIFICATION_MIN_SAMPLES", 36))

        if measured.sample_count < min_samples:
            baseline = dict(record.baseline or {})
            if not baseline.get("verification_extended") and record.verification_status != "inconclusive":
                baseline["verification_extended"] = True
                updated = await self.lifecycle.transition(
                    record.id,
                    "verifying",
                    event_type="verification_started",
                    updates={
                        "baseline": baseline,
                        "verification_status": "in_progress",
                        "verification_window_start": window_start,
                        "verification_window_end": now + timedelta(hours=window_hours),
                    },
                    payload={"reason": "insufficient_samples_extended"},
                )
                return updated
            return await self.lifecycle.mark_inconclusive(
                record.id,
                reason=f"insufficient samples after extension ({measured.sample_count} < {min_samples})",
                verification_window_start=window_start,
                verification_window_end=now,
            )

        outcome = self._evaluate(record, measured, window_start, now)

        if outcome.outcome == "rollback_review":
            return await self.lifecycle.mark_rollback_review(
                record.id,
                reasons=outcome.reasons,
                verification_window_start=window_start,
                verification_window_end=now,
                measured_co2e_saved_grams=outcome.measured_co2e_saved_grams,
                measured_cost_saved=outcome.measured_cost_saved,
            )
        if outcome.outcome == "verified":
            return await self.lifecycle.mark_verified(
                record.id,
                measured_co2e_saved_grams=outcome.measured_co2e_saved_grams,
                measured_cost_saved=outcome.measured_cost_saved,
                verification_window_start=window_start,
                verification_window_end=now,
            )
        return await self.lifecycle.mark_not_realized(
            record.id,
            reasons=outcome.reasons,
            measured_co2e_saved_grams=outcome.measured_co2e_saved_grams,
            measured_cost_saved=outcome.measured_cost_saved,
            verification_window_start=window_start,
            verification_window_end=now,
        )

    async def _measure(self, record: RecommendationRecord, start: datetime, end: datetime) -> _Measured:
        """Reads post-apply metrics for the recommendation target."""
        result = _Measured()
        if not record.namespace:
            return result

        try:
            metrics = await self.combined_repo.read_combined_metrics_smart(
                start_time=start, end_time=end, namespace=record.namespace
            )
        except Exception as exc:
            logger.warning("Could not read metrics for verification of %s: %s", record.id, exc)
            return result

        series = _series_for_record(record, metrics)
        if not series:
            return result

        window_seconds = max((end - start).total_seconds(), 1.0)
        total_cost = sum(m.total_cost for m in series if m.total_cost is not None)
        total_co2 = sum(m.co2e_grams for m in series if m.co2e_grams is not None)
        result.cost_per_hour_after = total_cost / window_seconds * 3600
        result.co2e_grams_per_hour_after = total_co2 / window_seconds * 3600

        cpu_points = [m.cpu_usage_millicores for m in series if m.cpu_usage_millicores is not None]
        memory_points = [m.memory_usage_bytes for m in series if m.memory_usage_bytes is not None]
        if cpu_points:
            result.cpu_p95 = percentile(cpu_points, 95)
        if memory_points:
            result.memory_p95 = percentile(memory_points, 95)

        result.restart_count = max((m.restart_count or 0 for m in series), default=0)
        result.sample_count = sum(max(int(m.sample_count or 1), 1) for m in series)
        return result

    def _evaluate(
        self,
        record: RecommendationRecord,
        measured: _Measured,
        window_start: datetime,
        window_end: datetime,
    ) -> VerificationOutcome:
        baseline = record.baseline or {}
        reasons: List[str] = []

        # --- Health gates -------------------------------------------------
        max_restart_delta = int(getattr(self.config, "VERIFICATION_MAX_RESTART_DELTA", 0))
        baseline_restarts = int(baseline.get("restart_count") or 0)
        if measured.restart_count - baseline_restarts > max_restart_delta:
            reasons.append(f"restart delta {measured.restart_count - baseline_restarts} exceeds {max_restart_delta}")

        headroom = float(getattr(self.config, "VERIFICATION_USAGE_HEADROOM", 1.1))
        proposed = baseline.get("proposed") or {}
        proposed_cpu = proposed.get("cpu_request_millicores") or record.recommended_cpu_request_millicores
        proposed_memory = proposed.get("memory_request_bytes") or record.recommended_memory_request_bytes

        if record.type == RecommendationType.RIGHTSIZING_CPU and proposed_cpu and measured.cpu_p95 is not None:
            if measured.cpu_p95 > proposed_cpu * headroom:
                reasons.append(f"cpu p95 {measured.cpu_p95:.0f}m exceeds proposed {proposed_cpu}m x {headroom}")
        if record.type == RecommendationType.RIGHTSIZING_MEMORY and proposed_memory and measured.memory_p95 is not None:
            if measured.memory_p95 > proposed_memory * headroom:
                reasons.append(
                    f"memory p95 {measured.memory_p95:.0f}B exceeds proposed {proposed_memory}B x {headroom}"
                )

        measured_cost, measured_co2 = self._annualized_savings(record, measured)

        if reasons:
            return VerificationOutcome(
                outcome="rollback_review",
                reasons=reasons,
                measured_co2e_saved_grams=measured_co2,
                measured_cost_saved=measured_cost,
                sample_count=measured.sample_count,
                window_start=window_start,
                window_end=window_end,
            )

        # --- Cost and carbon gates ---------------------------------------
        min_ratio = float(getattr(self.config, "VERIFICATION_MIN_SAVINGS_RATIO", 0.5))
        projected_cost = float(record.potential_savings_cost or 0.0)
        projected_co2 = float(record.potential_savings_co2e_grams or 0.0)

        savings_reasons: List[str] = []
        if projected_cost > 0 and measured_cost < min_ratio * projected_cost:
            savings_reasons.append(
                f"measured cost saving {measured_cost:.2f}/yr is below "
                f"{min_ratio:.0%} of the projected {projected_cost:.2f}/yr"
            )
        if projected_co2 > 0:
            baseline_co2_rate = baseline.get("co2e_grams_per_hour_before")
            after_co2_rate = measured.co2e_grams_per_hour_after
            if baseline_co2_rate is not None and after_co2_rate is not None and baseline_co2_rate > 0:
                measured_co2 = max((baseline_co2_rate - after_co2_rate) * (_SECONDS_PER_YEAR / 3600), 0.0)
            if measured_co2 < min_ratio * projected_co2:
                savings_reasons.append(
                    f"measured CO2e saving {measured_co2:.0f}g/yr is below "
                    f"{min_ratio:.0%} of the projected {projected_co2:.0f}g/yr"
                )

        if savings_reasons:
            return VerificationOutcome(
                outcome="not_realized",
                reasons=savings_reasons,
                measured_co2e_saved_grams=measured_co2,
                measured_cost_saved=measured_cost,
                sample_count=measured.sample_count,
                window_start=window_start,
                window_end=window_end,
            )

        return VerificationOutcome(
            outcome="verified",
            reasons=[],
            measured_co2e_saved_grams=measured_co2,
            measured_cost_saved=measured_cost,
            sample_count=measured.sample_count,
            window_start=window_start,
            window_end=window_end,
        )

    def _annualized_savings(self, record: RecommendationRecord, measured: _Measured) -> tuple[float, float]:
        """Annualizes the before/after cost and carbon reduction."""
        baseline = record.baseline or {}
        measured_cost = 0.0
        measured_co2 = 0.0

        before_cost = baseline.get("cost_per_hour_before")
        if before_cost is not None and measured.cost_per_hour_after is not None:
            measured_cost = max((before_cost - measured.cost_per_hour_after) * (_SECONDS_PER_YEAR / 3600), 0.0)

        before_co2 = baseline.get("co2e_grams_per_hour_before")
        if before_co2 is not None and measured.co2e_grams_per_hour_after is not None:
            measured_co2 = max((before_co2 - measured.co2e_grams_per_hour_after) * (_SECONDS_PER_YEAR / 3600), 0.0)

        return measured_cost, measured_co2


def _series_for_record(record: RecommendationRecord, metrics: Sequence) -> List:
    """Filters post-apply metrics down to the recommendation target."""
    from greenkube.core.optimization.targeting import group_by_recommendation_target

    grouped = group_by_recommendation_target(list(metrics))
    if record.owner_kind and record.owner_name:
        series = grouped.get((record.namespace or "", record.owner_kind, record.owner_name))
        if series:
            return series
    if record.pod_name:
        for key, series in grouped.items():
            if key[0] == record.namespace and key[2] == record.pod_name:
                return series
    return []
