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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from greenkube.core.optimization.statistics import percentile
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.models.verification import (
    KubernetesHealthObservation,
    MeasuredLedgerInput,
    TrafficSeasonalityControl,
)

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle
    from greenkube.storage.base_repository import CombinedMetricsRepository

logger = logging.getLogger(__name__)

_SECONDS_PER_YEAR = 365.25 * 24 * 3600

#: Statuses that are candidates for verification.
VERIFIABLE_STATUSES = ["applied", "verifying"]

#: Carbon projections below this annual value (grams) are not meaningful enough
#: to fail a verification: measurement noise dominates such tiny figures.
_MIN_MEANINGFUL_CO2_GRAMS_ANNUAL = 1.0


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
    ledger_input: Optional[MeasuredLedgerInput] = None


@dataclass
class _Measured:
    cost_per_hour_before: Optional[float] = None
    co2e_grams_per_hour_before: Optional[float] = None
    cost_per_hour_after: Optional[float] = None
    co2e_grams_per_hour_after: Optional[float] = None
    cpu_p95: Optional[float] = None
    memory_p95: Optional[float] = None
    restart_count: int = 0
    sample_count: int = 0
    oom_events: Optional[int] = None
    readiness_ratio: Optional[float] = None
    throttling_ratio: Optional[float] = None
    oom_kill_count: int = 0
    traffic: TrafficSeasonalityControl = field(default_factory=TrafficSeasonalityControl)


def _step_seconds(config: "Config") -> int:
    """Returns the Prometheus query step in seconds, defaulting to 5 minutes."""
    units = {"s": 1, "m": 60, "h": 3600}
    raw = (getattr(config, "PROMETHEUS_QUERY_RANGE_STEP", "") or "5m").strip().lower()
    try:
        return int(raw[:-1]) * units[raw[-1]]
    except (ValueError, KeyError, IndexError):
        return 300


def _sample_rate(total: float, sample_count: int, step_seconds: int) -> Optional[float]:
    """Converts a summed metric into a per-hour rate normalized by sample duration.

    Normalizing by the observed sample duration (count x step) rather than the
    wall-clock window keeps before/after rates comparable even when collection
    is sparse (e.g. a freshly installed instance).
    """
    if sample_count <= 0 or step_seconds <= 0:
        return None
    return total / (sample_count * step_seconds) * 3600


class RecommendationVerifier:
    """Evaluates applied recommendations once their observation window elapses."""

    def __init__(
        self,
        lifecycle: "RecommendationLifecycle",
        combined_repo: "CombinedMetricsRepository",
        config: Optional["Config"] = None,
        health_collector=None,
    ):
        from greenkube.core.config import get_config

        self.lifecycle = lifecycle
        self.combined_repo = combined_repo
        self.config = config if config is not None else get_config()
        self.health_collector = health_collector

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify_due(self, namespace: Optional[str] = None) -> List[RecommendationRecord]:
        """Verifies every applied recommendation whose window has elapsed."""
        records = await self.lifecycle.repo.get_recommendations_by_statuses(VERIFIABLE_STATUSES, namespace=namespace)
        # ``inconclusive`` is terminal for verification.  The lifecycle keeps
        # the recommendation applied for reporting, so exclude it explicitly.
        records = [r for r in records if r.verification_status != "inconclusive"]
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
        window_start = record.verification_window_start or record.applied_at
        window_end = record.verification_window_end or record.applied_at + timedelta(hours=window_hours)
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
                        "verification_window_end": window_end + timedelta(hours=window_hours),
                    },
                    payload={"reason": "insufficient_samples_extended"},
                )
                return updated
            from greenkube.core.observability import record_verification

            record_verification("inconclusive", ["insufficient_samples"])
            return await self.lifecycle.mark_inconclusive(
                record.id,
                reason=f"insufficient samples after extension ({measured.sample_count} < {min_samples})",
                verification_window_start=window_start,
                verification_window_end=now,
            )

        outcome = self._evaluate(record, measured, window_start, now)
        from greenkube.core.observability import record_verification

        record_verification(outcome.outcome, outcome.reasons if outcome.outcome == "rollback_review" else [])
        if outcome.ledger_input is not None:
            baseline = dict(record.baseline or {})
            baseline["measured_ledger_input"] = outcome.ledger_input.model_dump(mode="json")
            await self.lifecycle.repo.update_recommendation_fields(record.id, {"baseline": baseline})

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

    async def _read_series(self, record: RecommendationRecord, start: datetime, end: datetime) -> List:
        """Reads the metric series for a target in a time range (best-effort)."""
        if not record.namespace:
            return []
        try:
            metrics = await self.combined_repo.read_combined_metrics_smart(
                start_time=start, end_time=end, namespace=record.namespace
            )
        except Exception as exc:
            logger.warning("Could not read metrics for verification of %s: %s", record.id, exc)
            return []
        return _series_for_record(record, metrics)

    async def _measure(self, record: RecommendationRecord, start: datetime, end: datetime) -> _Measured:
        """Reads pre/post-apply metrics for the recommendation target."""
        result = _Measured()
        if not record.namespace:
            return result

        step = _step_seconds(self.config)
        after_series = await self._read_series(record, start, end)
        if not after_series:
            return result

        span = end - start
        before_series = await self._read_series(record, start - span, start)

        after_points = _aggregate_series(after_series)
        result.cost_per_hour_after = _sample_rate(
            sum(point["cost"] for point in after_points),
            sum(point["samples"] for point in after_points),
            step,
        )
        result.co2e_grams_per_hour_after = _sample_rate(
            sum(point["co2"] for point in after_points),
            sum(point["samples"] for point in after_points),
            step,
        )
        if before_series:
            before_points = _aggregate_series(before_series)
            result.cost_per_hour_before = _sample_rate(
                sum(point["cost"] for point in before_points),
                sum(point["samples"] for point in before_points),
                step,
            )
            result.co2e_grams_per_hour_before = _sample_rate(
                sum(point["co2"] for point in before_points),
                sum(point["samples"] for point in before_points),
                step,
            )

        cpu_points = [point["cpu"] for point in after_points if point["cpu"] is not None]
        memory_points = [point["memory"] for point in after_points if point["memory"] is not None]
        if cpu_points:
            result.cpu_p95 = percentile(cpu_points, 95)
        if memory_points:
            result.memory_p95 = percentile(memory_points, 95)

        result.restart_count = max((point["restarts"] for point in after_points), default=0)
        result.sample_count = sum(point["samples"] for point in after_points)
        result.oom_events = _optional_sum(after_series, ("oom_events", "oom_event_count"))
        result.readiness_ratio = _optional_min_or_avg(after_series, ("readiness_ratio", "readiness"))
        result.throttling_ratio = _optional_max_or_avg(
            after_series, ("cpu_throttling_ratio", "throttling_ratio", "cpu_throttle_ratio")
        )
        if self.health_collector is not None:
            try:
                health = await self.health_collector.collect(
                    namespace=record.namespace,
                    workload=record.owner_name or record.pod_name,
                )
                if isinstance(health, KubernetesHealthObservation):
                    result.restart_count = max(result.restart_count, health.restart_count)
                    result.oom_kill_count = health.oom_kill_count
                    result.readiness_ratio = health.readiness_ratio
            except Exception as exc:
                logger.warning("Could not read Kubernetes health for verification of %s: %s", record.id, exc)

        before_traffic = sum(
            (m.network_receive_bytes or 0.0) + (m.network_transmit_bytes or 0.0) for m in before_series
        )
        after_traffic = sum((m.network_receive_bytes or 0.0) + (m.network_transmit_bytes or 0.0) for m in after_series)
        if before_traffic > 0 and after_traffic > 0:
            result.traffic = TrafficSeasonalityControl(
                before_traffic=before_traffic,
                after_traffic=after_traffic,
                seasonality_factor=float((record.baseline or {}).get("seasonality_factor", 1.0) or 1.0),
            )
            ratio = result.traffic.traffic_ratio
            factor = ratio * result.traffic.seasonality_factor if ratio else None
            if factor and factor > 0:
                result.cost_per_hour_after = (result.cost_per_hour_after or 0.0) / factor
                result.co2e_grams_per_hour_after = (result.co2e_grams_per_hour_after or 0.0) / factor
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
        min_readiness = float(getattr(self.config, "VERIFICATION_MIN_READINESS", 0.99))
        if measured.readiness_ratio is not None and measured.readiness_ratio < min_readiness:
            reasons.append(f"readiness ratio {measured.readiness_ratio:.2%} is below {min_readiness:.2%}")
        if measured.oom_kill_count > 0:
            reasons.append(f"{measured.oom_kill_count} OOM kill(s) observed after apply")

        self._add_optional_health_gates(measured, reasons, baseline)

        measured_cost, measured_co2 = self._annualized_savings(record, measured)
        ledger_input = MeasuredLedgerInput(
            before_cost_per_hour=measured.cost_per_hour_before,
            after_cost_per_hour=measured.cost_per_hour_after,
            before_co2e_grams_per_hour=measured.co2e_grams_per_hour_before,
            after_co2e_grams_per_hour=measured.co2e_grams_per_hour_after,
            sample_count=measured.sample_count,
            readiness_ratio=measured.readiness_ratio,
            traffic_ratio=measured.traffic.traffic_ratio,
            seasonality_factor=measured.traffic.seasonality_factor,
        )

        if reasons:
            return VerificationOutcome(
                outcome="rollback_review",
                reasons=reasons,
                measured_co2e_saved_grams=measured_co2,
                measured_cost_saved=measured_cost,
                sample_count=measured.sample_count,
                window_start=window_start,
                window_end=window_end,
                ledger_input=ledger_input,
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
        if projected_co2 >= _MIN_MEANINGFUL_CO2_GRAMS_ANNUAL:
            baseline_co2_rate = (
                measured.co2e_grams_per_hour_before
                if measured.co2e_grams_per_hour_before is not None
                else baseline.get("co2e_grams_per_hour_before")
            )
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
                ledger_input=ledger_input,
            )

        return VerificationOutcome(
            outcome="verified",
            reasons=[],
            measured_co2e_saved_grams=measured_co2,
            measured_cost_saved=measured_cost,
            sample_count=measured.sample_count,
            window_start=window_start,
            window_end=window_end,
            ledger_input=ledger_input,
        )

    def _add_optional_health_gates(self, measured: _Measured, reasons: List[str], baseline: Any) -> None:
        """Apply optional health gates only when the collector supplied them."""
        missing = []
        max_oom = baseline.get("oom_events", 0)
        if measured.oom_events is None:
            missing.append("oom_events")
        elif max_oom is not None and measured.oom_events > int(max_oom):
            reasons.append(f"oom events {measured.oom_events} exceeds baseline {max_oom}")

        minimum_readiness = getattr(
            self.config, "VERIFICATION_MIN_READINESS_RATIO", getattr(self.config, "VERIFICATION_MIN_READINESS", None)
        )
        if measured.readiness_ratio is None:
            missing.append("readiness_ratio")
        elif minimum_readiness is not None and measured.readiness_ratio < float(minimum_readiness):
            reasons.append(f"readiness ratio {measured.readiness_ratio:.3f} below {float(minimum_readiness):.3f}")

        maximum_throttling = getattr(
            self.config,
            "VERIFICATION_MAX_THROTTLING_RATIO",
            getattr(self.config, "VERIFICATION_MAX_THROTTLE_RATIO", None),
        )
        if measured.throttling_ratio is None:
            missing.append("throttling_ratio")
        elif maximum_throttling is not None and measured.throttling_ratio > float(maximum_throttling):
            reasons.append(
                f"CPU throttling ratio {measured.throttling_ratio:.3f} exceeds {float(maximum_throttling):.3f}"
            )
        if missing:
            logger.info("Verification optional health inputs unavailable: %s", ", ".join(missing))

    def _annualized_savings(self, record: RecommendationRecord, measured: _Measured) -> tuple[float, float]:
        """Annualizes the before/after cost and carbon reduction."""
        baseline = record.baseline or {}
        measured_cost = 0.0
        measured_co2 = 0.0

        before_cost = (
            measured.cost_per_hour_before
            if measured.cost_per_hour_before is not None
            else baseline.get("cost_per_hour_before")
        )
        if before_cost is not None and measured.cost_per_hour_after is not None:
            measured_cost = max((before_cost - measured.cost_per_hour_after) * (_SECONDS_PER_YEAR / 3600), 0.0)

        before_co2 = (
            measured.co2e_grams_per_hour_before
            if measured.co2e_grams_per_hour_before is not None
            else baseline.get("co2e_grams_per_hour_before")
        )
        if before_co2 is not None and measured.co2e_grams_per_hour_after is not None:
            measured_co2 = max((before_co2 - measured.co2e_grams_per_hour_after) * (_SECONDS_PER_YEAR / 3600), 0.0)

        return measured_cost, measured_co2


def _aggregate_series(series: Sequence) -> List[dict]:
    """Aggregate replica points into one timestamped workload point."""
    buckets: Dict[object, dict] = {}
    for index, metric in enumerate(series):
        key = metric.timestamp or index
        bucket = buckets.setdefault(
            key,
            {
                "cost": 0.0,
                "co2": 0.0,
                "cpu": 0.0,
                "memory": 0.0,
                "restarts": 0,
                "samples": 0,
                "cpu_present": False,
                "memory_present": False,
            },
        )
        bucket["cost"] += float(metric.total_cost or 0.0)
        bucket["co2"] += float(metric.co2e_grams or 0.0)
        samples = max(int(metric.sample_count or 1), 1)
        bucket["samples"] = max(bucket["samples"], samples)
        if metric.cpu_usage_millicores is not None:
            bucket["cpu"] += float(metric.cpu_usage_millicores)
            bucket["cpu_present"] = True
        if metric.memory_usage_bytes is not None:
            bucket["memory"] += float(metric.memory_usage_bytes)
            bucket["memory_present"] = True
        bucket["restarts"] += int(metric.restart_count or 0)
    for bucket in buckets.values():
        if not bucket["cpu_present"]:
            bucket["cpu"] = None
        if not bucket["memory_present"]:
            bucket["memory"] = None
        del bucket["cpu_present"]
        del bucket["memory_present"]
    return list(buckets.values())


def _optional_values(series: Sequence, names: tuple[str, ...]) -> Optional[List[float]]:
    values = []
    found = False
    for metric in series:
        for name in names:
            value = getattr(metric, name, None)
            if value is not None:
                found = True
                values.append(float(value))
                break
    return values if found else None


def _optional_sum(series: Sequence, names: tuple[str, ...]) -> Optional[int]:
    values = _optional_values(series, names)
    return int(sum(values)) if values is not None else None


def _optional_min_or_avg(series: Sequence, names: tuple[str, ...]) -> Optional[float]:
    values = _optional_values(series, names)
    return min(values) if values else None


def _optional_max_or_avg(series: Sequence, names: tuple[str, ...]) -> Optional[float]:
    values = _optional_values(series, names)
    return max(values) if values else None


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
