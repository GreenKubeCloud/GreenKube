# src/greenkube/core/optimization/verifier.py
"""Post-apply verification of recommendation outcomes (interim C0 measurement).

Verification separates "apply succeeded" from "recommendation succeeded". Each
applied recommendation gets **one fixed-horizon measurement**:

* after-window ``[rollout completion, + VERIFICATION_WINDOW_HOURS)``, extended
  once when data coverage is insufficient;
* baseline window of the same length, ending at the PR merge or plan approval
  and never overlapping the after-window (C0.12);
* only trusted rows: cost provenance (``cost_source``) when the rows carry it,
  otherwise ``total_cost > 0 and is_estimated is False`` (C0.3);
* rates normalized by the time the rows cover; traffic normalization is
  disabled until the load-driver counterfactual ships (C0.1, BUG-27);
* health gates per replica and per-pod restart deltas (C0.2, BUG-28), plus
  OOM kills, readiness and CPU throttling from a health collector; a missing
  signal is reported ``not_available``, never silently passed (C0.9, BUG-35);
* CPU and memory records of one container changed by one plan are measured
  once and the cost delta is split (C0.10, BUG-36);
* rightsizing never claims a CO2e saving: the carbon model is usage-based, so
  the measured CO2e delta is reported but not claimed;
* the projection is not a gate: a change is ``realized`` when the measured
  saving clears the noise floor, and projection accuracy is only reported;
* every decision is an immutable ``savings_measurements`` row (C0.13);
  ``not_realized`` and ``inconclusive`` are terminal for the measurement and
  only ``POST /recommendations/{id}/remeasure`` starts a new one (C0.5).

Outcomes on the recommendation: ``verified``, ``rollback_review``,
``inconclusive`` or ``applied`` with ``verification_status=failed``
(not realized).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from greenkube.models.measurement import (
    MeasurementOutcome,
    QuantityEstimate,
    QuantityOutcome,
    SavingsMeasurement,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.verification import (
    HealthGateResult,
    HealthGateStatus,
    KubernetesHealthObservation,
    MeasuredLedgerInput,
    WorkloadHealthObservation,
)
from greenkube.storage.base_repository import ContainerMetricsRepository
from greenkube.utils.time import ensure_utc

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle
    from greenkube.storage.base_repository import CombinedMetricsRepository
    from greenkube.storage.measurement_repository import SavingsMeasurementRepository

logger = logging.getLogger(__name__)

_SECONDS_PER_YEAR = 365.25 * 24 * 3600
_HOURS_PER_YEAR = _SECONDS_PER_YEAR / 3600

#: Statuses that are candidates for verification.
VERIFIABLE_STATUSES = ["applied", "verifying"]

#: Verification statuses of a measurement that has not been decided yet. A
#: ``failed`` (not realized), ``inconclusive`` or ``passed`` measurement is
#: terminal: re-testing it until it passes would bias the outcome (BUG-31).
AWAITING_MEASUREMENT = frozenset({None, "", "pending", "in_progress"})

#: Noise floor of the interim outcome rule: a saving smaller than this share of
#: the baseline rate is ``not_realized``. C4 replaces it with a bootstrap CI.
MIN_RELATIVE_CHANGE = 0.02

#: Records without a plan whose changes landed this close together are one change.
GROUPING_TOLERANCE = timedelta(minutes=15)

#: Cost provenance trusted for savings (A11 ``CostSource``).
TRUSTED_COST_SOURCES = frozenset({"opencost", "kubecost", "pricing_catalog"})
#: Usage provenance trusted for health gates (A11 ``UsageSource``).
TRUSTED_USAGE_SOURCES = frozenset({"prometheus"})
#: Carbon-intensity provenance trusted for CO2e savings (A11 ``IntensitySource``).
TRUSTED_INTENSITY_SOURCES = frozenset({"realtime", "consolidated"})

_RIGHTSIZING_TYPES = frozenset({RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY})

_OUTCOME_LABELS = {
    MeasurementOutcome.REALIZED: "verified",
    MeasurementOutcome.NOT_REALIZED: "not_realized",
    MeasurementOutcome.INCONCLUSIVE: "inconclusive",
    MeasurementOutcome.ROLLBACK_REVIEW: "rollback_review",
}


# ----------------------------------------------------------------------
# Provenance helpers (C0.3)
# ----------------------------------------------------------------------


def _source(metric: Any, name: str) -> Optional[str]:
    value = getattr(metric, name, None)
    if value is None or value == "":
        return None
    return str(getattr(value, "value", value))


def cost_is_trusted(metric: Any) -> bool:
    """Whether a row's cost may back a savings measurement.

    Rows carrying cost provenance (A11) are trusted for OpenCost, Kubecost and
    pricing-catalog costs. Legacy rows fall back to ``total_cost > 0`` and an
    explicit ``is_estimated is False``.
    """
    source = _source(metric, "cost_source")
    if source is not None:
        return source in TRUSTED_COST_SOURCES
    return float(getattr(metric, "total_cost", 0.0) or 0.0) > 0 and getattr(metric, "is_estimated", None) is False


def carbon_is_trusted(metric: Any) -> bool:
    """Whether a row's CO2e may back a claimed carbon saving."""
    if _source(metric, "power_source") == "default_profile":
        return False
    source = _source(metric, "intensity_source")
    if source is not None:
        return source in TRUSTED_INTENSITY_SOURCES
    return getattr(metric, "is_estimated", None) is False


def usage_is_trusted(metric: Any) -> bool:
    """Whether a row's usage may back a health gate (``usage_source=prometheus`` when known)."""
    source = _source(metric, "usage_source")
    return source is None or source in TRUSTED_USAGE_SOURCES


# ----------------------------------------------------------------------
# Window arithmetic
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementWindows:
    """Baseline and after windows of one measurement (half-open)."""

    baseline_start: datetime
    baseline_end: datetime
    after_start: datetime
    after_end: datetime

    def as_dict(self) -> dict:
        return {
            "baseline": {"start": self.baseline_start.isoformat(), "end": self.baseline_end.isoformat()},
            "after": {"start": self.after_start.isoformat(), "end": self.after_end.isoformat()},
        }


@dataclass
class WindowRates:
    """Hourly rates of one window, computed over trusted, complete timestamp groups."""

    cost_per_hour: Optional[float] = None
    cost_cpu_per_hour: Optional[float] = None
    cost_ram_per_hour: Optional[float] = None
    co2e_per_hour: Optional[float] = None
    cost_steps: float = 0.0
    co2e_steps: float = 0.0
    traffic_bytes_per_second: Optional[float] = None


def _step_seconds(config: "Config") -> int:
    """Returns the Prometheus query step in seconds, defaulting to 5 minutes."""
    units = {"s": 1, "m": 60, "h": 3600}
    raw = (getattr(config, "PROMETHEUS_QUERY_RANGE_STEP", "") or "5m").strip().lower()
    try:
        return int(raw[:-1]) * units[raw[-1]]
    except (ValueError, KeyError, IndexError):
        return 300


def _coverage_seconds(metric: Any, step: int) -> float:
    """Time covered by one row: its samples x step, or its duration when larger.

    Hourly rows whose reader dropped ``sample_count`` still carry the summed
    ``duration_seconds``, so their cost is not mistaken for a single sample.
    """
    samples = max(int(getattr(metric, "sample_count", 1) or 1), 1)
    coverage = float(samples * step)
    duration = getattr(metric, "duration_seconds", None)
    if duration and float(duration) > coverage:
        coverage = float(duration)
    return coverage


def _timestamp_groups(series: Sequence) -> Dict[object, List]:
    groups: Dict[object, List] = defaultdict(list)
    for index, metric in enumerate(series):
        key = metric.timestamp if metric.timestamp is not None else ("row", index)
        groups[key].append(metric)
    return groups


def coverage_steps(series: Sequence, step: int) -> float:
    """Number of steps covered by a series, counting each timestamp once."""
    if step <= 0:
        return 0.0
    return sum(max(_coverage_seconds(m, step) for m in rows) for rows in _timestamp_groups(series).values()) / step


def window_rates(series: Sequence, step: int) -> WindowRates:
    """Per-hour cost, CO2e and traffic rates of a workload over a window.

    Replica rows are summed per timestamp; a timestamp group counts only when
    every row in it is trusted, so a partially estimated group never biases
    the sum. Rates divide by the covered time, never by row counts, so raw
    5-minute and hourly rows mix correctly. Traffic is a rate (bytes/s)
    averaged over covered time; it is informational only.
    """
    rates = WindowRates()
    cost_total = cpu_total = ram_total = co2_total = 0.0
    cost_cover = co2_cover = 0.0
    components = True
    traffic_total = traffic_cover = 0.0
    for rows in _timestamp_groups(series).values():
        coverage = max(_coverage_seconds(m, step) for m in rows)
        if all(cost_is_trusted(m) for m in rows):
            cost_total += sum(float(m.total_cost or 0.0) for m in rows)
            cost_cover += coverage
            cpu = [getattr(m, "cost_cpu", None) for m in rows]
            ram = [getattr(m, "cost_ram", None) for m in rows]
            if any(value is None for value in cpu + ram):
                components = False
            else:
                cpu_total += sum(float(value) for value in cpu if value is not None)
                ram_total += sum(float(value) for value in ram if value is not None)
        if all(carbon_is_trusted(m) for m in rows):
            co2_total += sum(float(m.co2e_grams or 0.0) for m in rows)
            co2_cover += coverage
        traffic = [
            float(m.network_receive_bytes or 0.0) + float(m.network_transmit_bytes or 0.0)
            for m in rows
            if m.network_receive_bytes is not None or m.network_transmit_bytes is not None
        ]
        if traffic:
            traffic_total += sum(traffic) * coverage
            traffic_cover += coverage
    if cost_cover > 0:
        rates.cost_per_hour = cost_total / cost_cover * 3600
        # Components that are absent, or all zero while the cost is not, cannot split a delta.
        if components and (cpu_total + ram_total > 0 or cost_total <= 0):
            rates.cost_cpu_per_hour = cpu_total / cost_cover * 3600
            rates.cost_ram_per_hour = ram_total / cost_cover * 3600
    if co2_cover > 0:
        rates.co2e_per_hour = co2_total / co2_cover * 3600
    if traffic_cover > 0:
        rates.traffic_bytes_per_second = traffic_total / traffic_cover
    rates.cost_steps = cost_cover / step if step > 0 else 0.0
    rates.co2e_steps = co2_cover / step if step > 0 else 0.0
    return rates


def _weighted_percentile(pairs: List[tuple[float, float]], p: float) -> Optional[float]:
    if not pairs:
        return None
    ordered = sorted(pairs)
    total = sum(weight for _, weight in ordered)
    threshold = total * p / 100.0
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= threshold:
            return value
    return ordered[-1][0]


def per_pod_usage_p95(series: Sequence, attribute: str, step: int) -> Optional[float]:
    """p95 over per-pod usage samples (never the replica sum, BUG-28)."""
    pairs = []
    for metric in series:
        value = getattr(metric, attribute, None)
        if value is None or not usage_is_trusted(metric):
            continue
        pairs.append((float(value), _coverage_seconds(metric, step) / max(step, 1)))
    return _weighted_percentile(pairs, 95)


def restart_increase(series: Sequence) -> Optional[int]:
    """Sum of per-pod increases of the cumulative restart counter.

    Each pod starts from its first observation in the window; a decrease is a
    counter reset (a recreated pod with the same name) and counts from zero.
    """
    observations: Dict[str, List[tuple[datetime, int, int]]] = defaultdict(list)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for index, metric in enumerate(series):
        if metric.restart_count is None:
            continue
        moment = ensure_utc(metric.timestamp) if metric.timestamp is not None else floor
        observations[metric.pod_name].append((moment, index, int(metric.restart_count)))
    if not observations:
        return None
    total = 0
    for values in observations.values():
        values.sort()
        previous = values[0][2]
        for _, _, current in values[1:]:
            total += current - previous if current >= previous else current
            previous = current
    return total


# ----------------------------------------------------------------------
# Attribution groups (C0.10)
# ----------------------------------------------------------------------


def _baseline_dict(record: RecommendationRecord) -> dict:
    baseline = record.baseline
    if baseline is None:
        return {}
    if hasattr(baseline, "model_dump"):
        return baseline.model_dump(mode="json")
    return dict(baseline)


def _anchor(record: RecommendationRecord) -> datetime:
    start = record.verification_window_start or record.applied_at
    return ensure_utc(start) if start is not None else datetime.min.replace(tzinfo=timezone.utc)


def _joins_group(group: List[RecommendationRecord], record: RecommendationRecord) -> bool:
    if record.type not in _RIGHTSIZING_TYPES:
        return False
    if any(member.type not in _RIGHTSIZING_TYPES or member.type == record.type for member in group):
        return False
    lead = group[0]
    if abs(_anchor(record) - _anchor(lead)) > GROUPING_TOLERANCE:
        return False
    lead_plan = _baseline_dict(lead).get("plan_id")
    record_plan = _baseline_dict(record).get("plan_id")
    return lead_plan == record_plan


def attribution_groups(records: Sequence[RecommendationRecord]) -> List[List[RecommendationRecord]]:
    """Group the CPU and memory records that one change applied to one container.

    Records join when they target the same container, were applied by the same
    plan (or, without a plan, landed within :data:`GROUPING_TOLERANCE`) and are
    measured over the same window. Everything else is measured alone.
    """
    by_target: Dict[tuple, List[RecommendationRecord]] = defaultdict(list)
    for record in records:
        key = (
            record.namespace,
            record.owner_kind or "Pod",
            record.owner_name or record.pod_name,
            record.container_name,
        )
        by_target[key].append(record)
    groups: List[List[RecommendationRecord]] = []
    for members in by_target.values():
        members.sort(key=lambda item: (_anchor(item), item.id or 0))
        current: List[RecommendationRecord] = []
        for record in members:
            if current and _joins_group(current, record):
                current.append(record)
                continue
            if current:
                groups.append(current)
            current = [record]
        if current:
            groups.append(current)
    return groups


def _projected_share(record: RecommendationRecord, group: Sequence[RecommendationRecord]) -> float:
    projections = [max(float(member.potential_savings_cost or 0.0), 0.0) for member in group]
    total = sum(projections)
    if total <= 0:
        return 1.0 / len(group)
    return max(float(record.potential_savings_cost or 0.0), 0.0) / total


# ----------------------------------------------------------------------
# Verifier
# ----------------------------------------------------------------------


@dataclass
class _Observation:
    scope: str
    before: WindowRates
    after: WindowRates
    sample_steps: float = 0.0
    cpu_p95: Optional[float] = None
    memory_p95: Optional[float] = None
    restart_increase: Optional[int] = None
    pods: List[str] = field(default_factory=list)
    health: Optional[WorkloadHealthObservation] = None
    traffic_ratio: Optional[float] = None


@dataclass
class _Decision:
    outcome: MeasurementOutcome
    reasons: List[str]
    estimates: List[QuantityEstimate]


class RecommendationVerifier:
    """Evaluates applied recommendations once their observation window elapses."""

    def __init__(
        self,
        lifecycle: "RecommendationLifecycle",
        combined_repo: "CombinedMetricsRepository",
        config: Optional["Config"] = None,
        health_collector=None,
        measurement_repo: Optional["SavingsMeasurementRepository"] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        from greenkube.core.config import get_config

        self.lifecycle = lifecycle
        self.combined_repo = combined_repo
        self.config = config if config is not None else get_config()
        self.health_collector = health_collector
        self.measurement_repo = measurement_repo if measurement_repo is not None else _default_repo(lifecycle)
        self._clock = clock

    def _now(self) -> datetime:
        return ensure_utc(self._clock()) if self._clock is not None else datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify_due(self, namespace: Optional[str] = None) -> List[RecommendationRecord]:
        """Measures every applied recommendation whose fixed window has elapsed."""
        records = await self.lifecycle.repo.get_recommendations_by_statuses(VERIFIABLE_STATUSES, namespace=namespace)
        pending = [
            record
            for record in records
            if record.id is not None
            and record.applied_at is not None
            and record.verification_status in AWAITING_MEASUREMENT
        ]
        now = self._now()
        updated: List[RecommendationRecord] = []
        for group in attribution_groups(pending):
            try:
                updated.extend(await self._verify_group(group, now))
            except Exception as exc:
                logger.warning(
                    "Verification failed for recommendation(s) %s: %s",
                    ", ".join(str(record.id) for record in group),
                    exc,
                )
        return updated

    # ------------------------------------------------------------------
    # Group verification
    # ------------------------------------------------------------------

    def _window_hours(self) -> float:
        return float(getattr(self.config, "VERIFICATION_WINDOW_HOURS", 72))

    def _min_samples(self) -> int:
        return int(getattr(self.config, "VERIFICATION_MIN_SAMPLES", 36))

    def windows_for(self, group: Sequence[RecommendationRecord]) -> MeasurementWindows:
        """Fixed after-window and its non-overlapping baseline window for a group."""
        window = timedelta(hours=self._window_hours())
        starts = [_anchor(record) for record in group]
        after_start = max(starts)
        ends = [
            ensure_utc(record.verification_window_end) if record.verification_window_end is not None else start + window
            for record, start in zip(group, starts)
        ]
        after_end = max(max(ends), after_start)
        baseline_ends = []
        for record in group:
            value = _baseline_dict(record).get("baseline_window_end")
            try:
                baseline_ends.append(ensure_utc(value) if value else ensure_utc(record.applied_at))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                baseline_ends.append(ensure_utc(record.applied_at))  # type: ignore[arg-type]
        baseline_end = min(min(baseline_ends), after_start)
        return MeasurementWindows(
            baseline_start=baseline_end - (after_end - after_start),
            baseline_end=baseline_end,
            after_start=after_start,
            after_end=after_end,
        )

    async def _verify_group(self, group: List[RecommendationRecord], now: datetime) -> List[RecommendationRecord]:
        window_hours = self._window_hours()
        windows = self.windows_for(group)
        grace = timedelta(hours=max(window_hours * 0.1, 0.05))
        if now < windows.after_end + grace:
            return []

        observation = await self._observe(group, windows)
        gates = self._health_gates(group, observation)
        failed = [gate for gate in gates if gate.status is HealthGateStatus.FAILED]
        min_samples = self._min_samples()

        if failed:
            decisions = [self._decide(record, group, observation, failed, min_samples) for record in group]
        elif observation.sample_steps < min_samples:
            return await self._extend_or_close(
                group,
                windows,
                observation,
                gates,
                now,
                reason="insufficient_samples",
                detail=f"({observation.sample_steps:.0f} < {min_samples})",
            )
        else:
            decisions = [self._decide(record, group, observation, failed, min_samples) for record in group]
            if any(decision.outcome is MeasurementOutcome.INCONCLUSIVE for decision in decisions):
                trusted = min(observation.before.cost_steps, observation.after.cost_steps)
                return await self._extend_or_close(
                    group,
                    windows,
                    observation,
                    gates,
                    now,
                    reason="insufficient_trusted_savings_samples",
                    detail=f"({trusted:.0f} < {min_samples})",
                )

        group_id = uuid.uuid4().hex
        return [
            await self._conclude(record, decision, group, group_id, windows, observation, gates, now)
            for record, decision in zip(group, decisions)
        ]

    async def _extend_or_close(
        self,
        group: List[RecommendationRecord],
        windows: MeasurementWindows,
        observation: _Observation,
        gates: List[HealthGateResult],
        now: datetime,
        *,
        reason: str,
        detail: str,
    ) -> List[RecommendationRecord]:
        """Extend the window once for missing data, then close the measurement as inconclusive."""
        if not any(_baseline_dict(record).get("verification_extended") for record in group):
            extension = timedelta(hours=self._window_hours())
            extended: List[RecommendationRecord] = []
            for record in group:
                assert record.id is not None
                baseline = _baseline_dict(record)
                baseline["verification_extended"] = True
                extended.append(
                    await self.lifecycle.transition(
                        record.id,
                        RecommendationStatus.VERIFYING,
                        event_type="verification_started",
                        updates={
                            "baseline": baseline,
                            "verification_status": "in_progress",
                            "verification_window_start": _anchor(record),
                            "verification_window_end": windows.after_end + extension,
                        },
                        payload={"reason": f"{reason}_extended", "detail": detail},
                        expected_statuses=(RecommendationStatus.APPLIED, RecommendationStatus.VERIFYING),
                    )
                )
            return extended

        min_samples = self._min_samples()
        group_id = uuid.uuid4().hex
        closed: List[RecommendationRecord] = []
        for record in group:
            decision = self._decide(record, group, observation, [], min_samples)
            decision = _Decision(
                outcome=MeasurementOutcome.INCONCLUSIVE,
                reasons=[f"{reason} after extension {detail}"],
                estimates=decision.estimates,
            )
            closed.append(await self._conclude(record, decision, group, group_id, windows, observation, gates, now))
        return closed

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def _read_series(self, record: RecommendationRecord, start: datetime, end: datetime) -> List:
        """Reads the metric series of a recommendation target (container rows for container targets)."""
        if not record.namespace:
            return []
        try:
            if record.container_name is not None:
                if not record.container_name or record.container_name.strip() != record.container_name:
                    return []
                if not isinstance(self.combined_repo, ContainerMetricsRepository):
                    logger.warning(
                        "Container-scoped verification is unavailable for recommendation %s: "
                        "repository has no container metrics reader.",
                        record.id,
                    )
                    return []
                metrics = await self.combined_repo.read_container_metrics_smart(
                    start_time=start,
                    end_time=end,
                    namespace=record.namespace,
                )
            else:
                metrics = await self.combined_repo.read_combined_metrics_smart(
                    start_time=start, end_time=end, namespace=record.namespace
                )
        except Exception as exc:
            logger.warning("Could not read metrics for verification of %s: %s", record.id, exc)
            return []
        return _series_for_record(record, metrics)

    async def _read_pod_series(self, record: RecommendationRecord, start: datetime, end: datetime) -> List:
        """Reads the pod-level series of the record's workload (restarts and cost fallback)."""
        return await self._read_series(record.model_copy(update={"container_name": None}), start, end)

    async def _observe(self, group: List[RecommendationRecord], windows: MeasurementWindows) -> _Observation:
        lead = group[0]
        step = _step_seconds(self.config)
        min_samples = self._min_samples()
        target_after = await self._read_series(lead, windows.after_start, windows.after_end)
        target_before = await self._read_series(lead, windows.baseline_start, windows.baseline_end)
        if lead.container_name is not None:
            pod_after = await self._read_pod_series(lead, windows.after_start, windows.after_end)
            pod_before = await self._read_pod_series(lead, windows.baseline_start, windows.baseline_end)
        else:
            pod_after, pod_before = target_after, target_before

        before = window_rates(target_before, step)
        after = window_rates(target_after, step)
        if lead.container_name is None:
            scope = "workload" if lead.owner_kind and lead.owner_name else "pod"
        elif min(before.cost_steps, after.cost_steps) >= min_samples:
            scope = "container"
        else:
            # Container rows have no trusted cost (no provenance yet, BUG-29): measure the
            # container's change on its workload, where the other containers did not change.
            scope = "workload_fallback"
            before = window_rates(pod_before, step)
            after = window_rates(pod_after, step)

        pod_before_rates = before if pod_before is target_before else window_rates(pod_before, step)
        pod_after_rates = after if pod_after is target_after else window_rates(pod_after, step)
        traffic_ratio = None
        if pod_before_rates.traffic_bytes_per_second and pod_after_rates.traffic_bytes_per_second is not None:
            traffic_ratio = pod_after_rates.traffic_bytes_per_second / pod_before_rates.traffic_bytes_per_second

        pods = sorted({metric.pod_name for metric in (pod_after or target_after) if metric.pod_name})
        observation = _Observation(
            scope=scope,
            before=before,
            after=after,
            sample_steps=coverage_steps(target_after, step),
            cpu_p95=per_pod_usage_p95(target_after, "cpu_usage_millicores", step),
            memory_p95=per_pod_usage_p95(target_after, "memory_usage_bytes", step),
            restart_increase=restart_increase(pod_after),
            pods=pods,
            traffic_ratio=traffic_ratio,
        )
        observation.health = await self._collect_health(lead, pods, windows)
        return observation

    async def _collect_health(
        self,
        record: RecommendationRecord,
        pods: List[str],
        windows: MeasurementWindows,
    ) -> Optional[WorkloadHealthObservation]:
        if self.health_collector is None or not record.namespace:
            return None
        try:
            result = await self.health_collector.collect(
                namespace=record.namespace,
                pods=pods,
                container_name=record.container_name,
                start=windows.after_start,
                end=windows.after_end,
                owner_kind=record.owner_kind,
                owner_name=record.owner_name,
            )
        except Exception as exc:
            logger.warning("Could not read workload health for verification of %s: %s", record.id, exc)
            return None
        if isinstance(result, WorkloadHealthObservation):
            return result
        if isinstance(result, KubernetesHealthObservation):
            return WorkloadHealthObservation(
                oom_kill_count=result.oom_kill_count,
                readiness_ratio=result.readiness_ratio,
                source="kubernetes",
                pod_count=result.total_pods,
            )
        return None

    # ------------------------------------------------------------------
    # Gates and decisions
    # ------------------------------------------------------------------

    @staticmethod
    def _applied_request(record: RecommendationRecord, resource: str) -> Optional[float]:
        proposed = _baseline_dict(record).get("proposed") or {}
        if resource == "cpu":
            candidates = (
                record.actual_cpu_request_millicores,
                proposed.get("cpu_request_millicores"),
                record.recommended_cpu_request_millicores,
            )
        else:
            candidates = (
                record.actual_memory_request_bytes,
                proposed.get("memory_request_bytes"),
                record.recommended_memory_request_bytes,
            )
        for value in candidates:
            if value is not None and float(value) > 0:
                return float(value)
        return None

    def _health_gates(self, group: Sequence[RecommendationRecord], observation: _Observation) -> List[HealthGateResult]:
        """Evaluate every health gate; a missing signal is ``not_available``, never a pass."""
        config = self.config
        headroom = float(getattr(config, "VERIFICATION_USAGE_HEADROOM", 1.1))
        gates: List[HealthGateResult] = []
        for record in group:
            if record.type is RecommendationType.RIGHTSIZING_CPU:
                gates.append(
                    _usage_gate("cpu_p95", observation.cpu_p95, self._applied_request(record, "cpu"), headroom, "m")
                )
            elif record.type is RecommendationType.RIGHTSIZING_MEMORY:
                gates.append(
                    _usage_gate(
                        "memory_p95", observation.memory_p95, self._applied_request(record, "memory"), headroom, "B"
                    )
                )

        max_restarts = int(getattr(config, "VERIFICATION_MAX_RESTART_DELTA", 0))
        restarts = observation.restart_increase
        if restarts is None:
            gates.append(_not_available("restarts", "no restart counters observed"))
        elif restarts > max_restarts:
            gates.append(
                HealthGateResult(
                    gate="restarts",
                    status=HealthGateStatus.FAILED,
                    value=restarts,
                    threshold=max_restarts,
                    detail=f"restart delta {restarts} exceeds {max_restarts}",
                )
            )
        else:
            gates.append(
                HealthGateResult(
                    gate="restarts", status=HealthGateStatus.PASSED, value=restarts, threshold=max_restarts
                )
            )

        health = observation.health
        if self.health_collector is None:
            source = "health collector not configured"
        elif health is None:
            source = "health signals could not be read"
        else:
            source = "; ".join(health.notes) or "signal not available"
        oom = health.oom_kill_count if health is not None else None
        if oom is None:
            gates.append(_not_available("oom_kills", source))
        elif oom > 0:
            gates.append(
                HealthGateResult(
                    gate="oom_kills",
                    status=HealthGateStatus.FAILED,
                    value=oom,
                    threshold=0,
                    detail=f"{oom} OOM kill(s) observed after apply",
                )
            )
        else:
            gates.append(HealthGateResult(gate="oom_kills", status=HealthGateStatus.PASSED, value=0, threshold=0))

        min_readiness = float(getattr(config, "VERIFICATION_MIN_READINESS", 0.99))
        readiness = health.readiness_ratio if health is not None else None
        if readiness is None:
            gates.append(_not_available("readiness", source))
        elif readiness < min_readiness:
            gates.append(
                HealthGateResult(
                    gate="readiness",
                    status=HealthGateStatus.FAILED,
                    value=readiness,
                    threshold=min_readiness,
                    detail=f"readiness ratio {readiness:.2%} is below {min_readiness:.2%}",
                )
            )
        else:
            gates.append(
                HealthGateResult(
                    gate="readiness", status=HealthGateStatus.PASSED, value=readiness, threshold=min_readiness
                )
            )

        max_throttling = float(getattr(config, "VERIFICATION_MAX_THROTTLE_RATIO", 0.05))
        throttling = health.throttling_ratio if health is not None else None
        if throttling is None:
            gates.append(_not_available("cpu_throttling", source))
        elif throttling > max_throttling:
            gates.append(
                HealthGateResult(
                    gate="cpu_throttling",
                    status=HealthGateStatus.FAILED,
                    value=throttling,
                    threshold=max_throttling,
                    detail=f"CPU throttling ratio {throttling:.3f} exceeds {max_throttling:.3f}",
                )
            )
        else:
            gates.append(
                HealthGateResult(
                    gate="cpu_throttling", status=HealthGateStatus.PASSED, value=throttling, threshold=max_throttling
                )
            )
        return gates

    def _decide(
        self,
        record: RecommendationRecord,
        group: Sequence[RecommendationRecord],
        observation: _Observation,
        failed_gates: Sequence[HealthGateResult],
        min_samples: int,
    ) -> _Decision:
        cost = self._cost_estimate(record, group, observation, min_samples)
        co2e = self._co2e_estimate(record, group, observation, min_samples)
        estimates = [cost, co2e]
        claimed = [estimate for estimate in estimates if estimate.outcome is not QuantityOutcome.NOT_CLAIMED]
        notes = _projection_notes(estimates, float(getattr(self.config, "VERIFICATION_MIN_SAVINGS_RATIO", 0.5)))
        if failed_gates:
            return _Decision(MeasurementOutcome.ROLLBACK_REVIEW, [gate.detail for gate in failed_gates], estimates)
        if any(estimate.outcome is QuantityOutcome.REALIZED for estimate in claimed):
            return _Decision(MeasurementOutcome.REALIZED, notes, estimates)
        if not claimed or any(estimate.outcome is QuantityOutcome.INCONCLUSIVE for estimate in claimed):
            return _Decision(MeasurementOutcome.INCONCLUSIVE, ["insufficient_trusted_savings_samples"], estimates)
        reasons = [_not_realized_reason(estimate) for estimate in claimed]
        return _Decision(MeasurementOutcome.NOT_REALIZED, reasons + notes, estimates)

    def _cost_estimate(
        self,
        record: RecommendationRecord,
        group: Sequence[RecommendationRecord],
        observation: _Observation,
        min_samples: int,
    ) -> QuantityEstimate:
        before, after = observation.before, observation.after
        basis, share = "total", 1.0
        before_rate, after_rate = before.cost_per_hour, after.cost_per_hour
        components = before.cost_cpu_per_hour is not None and after.cost_cpu_per_hour is not None
        if record.type is RecommendationType.RIGHTSIZING_CPU and components:
            basis, before_rate, after_rate = "cost_cpu", before.cost_cpu_per_hour, after.cost_cpu_per_hour
        elif record.type is RecommendationType.RIGHTSIZING_MEMORY and components:
            basis, before_rate, after_rate = "cost_ram", before.cost_ram_per_hour, after.cost_ram_per_hour
        elif len(group) > 1:
            basis, share = "projected_share", _projected_share(record, group)
            before_rate = before_rate * share if before_rate is not None else None
            after_rate = after_rate * share if after_rate is not None else None
        enough = min(before.cost_steps, after.cost_steps) >= min_samples
        return _estimate(
            "cost",
            "currency",
            basis,
            share,
            before_rate,
            after_rate,
            before.cost_steps,
            after.cost_steps,
            enough,
            claimed=True,
            projected=record.potential_savings_cost,
        )

    def _co2e_estimate(
        self,
        record: RecommendationRecord,
        group: Sequence[RecommendationRecord],
        observation: _Observation,
        min_samples: int,
    ) -> QuantityEstimate:
        before, after = observation.before, observation.after
        share = _projected_share(record, group) if len(group) > 1 else 1.0
        before_rate = before.co2e_per_hour * share if before.co2e_per_hour is not None else None
        after_rate = after.co2e_per_hour * share if after.co2e_per_hour is not None else None
        enough = min(before.co2e_steps, after.co2e_steps) >= min_samples
        # Request rightsizing frees reserved capacity; the usage-based carbon model does not
        # move with requests, so its CO2e delta is reported, never claimed (BUG-16).
        return _estimate(
            "co2e_g",
            "gCO2e",
            "projected_share" if len(group) > 1 else "total",
            share,
            before_rate,
            after_rate,
            before.co2e_steps,
            after.co2e_steps,
            enough,
            claimed=record.type not in _RIGHTSIZING_TYPES,
            projected=record.potential_savings_co2e_grams,
        )

    # ------------------------------------------------------------------
    # Persistence and transitions
    # ------------------------------------------------------------------

    async def _conclude(
        self,
        record: RecommendationRecord,
        decision: _Decision,
        group: Sequence[RecommendationRecord],
        group_id: str,
        windows: MeasurementWindows,
        observation: _Observation,
        gates: List[HealthGateResult],
        now: datetime,
    ) -> RecommendationRecord:
        assert record.id is not None
        baseline = _baseline_dict(record)
        previous_ids = [str(item) for item in baseline.get("measurement_ids") or []]
        cost = _find(decision.estimates, "cost")
        co2e = _find(decision.estimates, "co2e_g")
        health = observation.health
        ledger_input = MeasuredLedgerInput(
            before_cost_per_hour=cost.before_per_hour if cost else None,
            after_cost_per_hour=cost.after_per_hour if cost else None,
            before_co2e_grams_per_hour=co2e.before_per_hour if co2e else None,
            after_co2e_grams_per_hour=co2e.after_per_hour if co2e else None,
            sample_count=int(observation.sample_steps),
            readiness_ratio=health.readiness_ratio if health is not None else None,
            traffic_ratio=observation.traffic_ratio,
            outcome=decision.outcome.value,
            cost_outcome=cost.outcome.value if cost else None,
            co2e_outcome=co2e.outcome.value if co2e else None,
            cost_basis=cost.basis if cost else None,
            measurement_scope=observation.scope,
            baseline_window_start=windows.baseline_start,
            baseline_window_end=windows.baseline_end,
            after_window_start=windows.after_start,
            after_window_end=windows.after_end,
            group_id=group_id,
        )
        measurement = SavingsMeasurement(
            measurement_id=uuid.uuid4().hex,
            recommendation_id=record.id,
            group_id=group_id,
            group_recommendation_ids=sorted(member.id for member in group if member.id is not None),
            plan_id=baseline.get("plan_id"),
            sequence=int(baseline.get("measurement_sequence") or 1),
            scope=observation.scope,
            windows=windows.as_dict(),
            estimates=decision.estimates,
            health=gates,
            ledger_input={},
            outcome=decision.outcome,
            reasons=decision.reasons,
            supersedes_measurement_id=previous_ids[-1] if previous_ids else None,
            decided_at=now,
        )
        ledger_input.measurement_id = measurement.measurement_id
        measurement = measurement.model_copy(
            update={"ledger_input": ledger_input.model_dump(mode="json")}
        ).with_evidence_digest()
        stored = await self._persist(measurement)
        return await self._apply(record, stored, baseline, previous_ids)

    async def _persist(self, measurement: SavingsMeasurement) -> SavingsMeasurement:
        if self.measurement_repo is None:
            logger.debug(
                "No measurement repository configured; measurement %s is not persisted.", measurement.measurement_id
            )
            return measurement
        return await self.measurement_repo.insert(measurement)

    async def _apply(
        self,
        record: RecommendationRecord,
        stored: SavingsMeasurement,
        baseline: dict,
        previous_ids: List[str],
    ) -> RecommendationRecord:
        """Drive the lifecycle from the stored (immutable) decision."""
        assert record.id is not None
        cost = _find(stored.estimates, "cost")
        co2e = _find(stored.estimates, "co2e_g")
        updated_baseline = dict(baseline)
        updated_baseline["measured_ledger_input"] = stored.ledger_input
        updated_baseline["measurement_ids"] = [*previous_ids, stored.measurement_id]
        updated_baseline["latest_measurement_id"] = stored.measurement_id
        window_start = ensure_utc(stored.windows["after"]["start"])
        window_end = ensure_utc(stored.windows["after"]["end"])
        details = {
            "measurement_id": stored.measurement_id,
            "outcome": stored.outcome.value,
            "health": [gate.model_dump(mode="json") for gate in stored.health],
        }
        failed_gates = [gate.gate for gate in stored.health if gate.status is HealthGateStatus.FAILED]
        missing = [gate.gate for gate in stored.health if gate.status is HealthGateStatus.NOT_AVAILABLE]
        if missing:
            logger.info("Verification of %s: health signals not available: %s", record.id, ", ".join(missing))

        from greenkube.core.observability import record_verification

        record_verification(_OUTCOME_LABELS[stored.outcome], failed_gates)
        measured_cost = _measured_value(cost, stored.outcome)
        measured_co2e = _measured_value(co2e, stored.outcome)
        if stored.outcome is MeasurementOutcome.ROLLBACK_REVIEW:
            return await self.lifecycle.mark_rollback_review(
                record.id,
                reasons=stored.reasons,
                verification_window_start=window_start,
                verification_window_end=window_end,
                measured_co2e_saved_grams=measured_co2e,
                measured_cost_saved=measured_cost,
                baseline=updated_baseline,
                details=details,
            )
        if stored.outcome is MeasurementOutcome.REALIZED:
            return await self.lifecycle.mark_verified(
                record.id,
                measured_co2e_saved_grams=measured_co2e if measured_co2e is not None else 0.0,
                measured_cost_saved=measured_cost if measured_cost is not None else 0.0,
                verification_window_start=window_start,
                verification_window_end=window_end,
                baseline=updated_baseline,
                details={**details, "notes": stored.reasons},
            )
        if stored.outcome is MeasurementOutcome.INCONCLUSIVE:
            return await self.lifecycle.mark_inconclusive(
                record.id,
                reason="; ".join(stored.reasons) or "inconclusive",
                verification_window_start=window_start,
                verification_window_end=window_end,
                baseline=updated_baseline,
                details=details,
            )
        return await self.lifecycle.mark_not_realized(
            record.id,
            reasons=stored.reasons,
            measured_co2e_saved_grams=measured_co2e,
            measured_cost_saved=measured_cost,
            verification_window_start=window_start,
            verification_window_end=window_end,
            baseline=updated_baseline,
            details=details,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _default_repo(lifecycle) -> Optional["SavingsMeasurementRepository"]:
    db_manager = getattr(getattr(lifecycle, "repo", None), "db_manager", None)
    if db_manager is None:
        return None
    from greenkube.storage.measurement_repository import SavingsMeasurementRepository

    return SavingsMeasurementRepository(db_manager)


def _not_available(gate: str, detail: str) -> HealthGateResult:
    return HealthGateResult(gate=gate, status=HealthGateStatus.NOT_AVAILABLE, detail=detail)


def _usage_gate(
    gate: str,
    p95: Optional[float],
    request: Optional[float],
    headroom: float,
    unit: str,
) -> HealthGateResult:
    if request is None:
        return _not_available(gate, "no applied request to compare with")
    threshold = request * headroom
    if p95 is None:
        return _not_available(gate, "no per-pod usage samples in the window")
    if p95 > threshold:
        return HealthGateResult(
            gate=gate,
            status=HealthGateStatus.FAILED,
            value=p95,
            threshold=threshold,
            detail=f"{gate.replace('_', ' ')} {p95:.0f}{unit} per pod exceeds request {request:.0f}{unit} x {headroom}",
        )
    return HealthGateResult(gate=gate, status=HealthGateStatus.PASSED, value=p95, threshold=threshold)


def _estimate(
    quantity: str,
    unit: str,
    basis: str,
    share: float,
    before: Optional[float],
    after: Optional[float],
    before_steps: float,
    after_steps: float,
    enough: bool,
    *,
    claimed: bool,
    projected: Optional[float],
) -> QuantityEstimate:
    values: dict[str, Any] = {
        "quantity": quantity,
        "unit": unit,
        "basis": basis,
        "share": share,
        "before_per_hour": before,
        "after_per_hour": after,
        "before_coverage_steps": before_steps,
        "after_coverage_steps": after_steps,
        "projected_annual": projected,
    }
    if not enough or before is None or after is None:
        return QuantityEstimate(
            **values, outcome=QuantityOutcome.NOT_CLAIMED if not claimed else QuantityOutcome.INCONCLUSIVE
        )
    delta = before - after
    annualized = delta * _HOURS_PER_YEAR
    realized = delta > 0 and delta > MIN_RELATIVE_CHANGE * abs(before)
    if not claimed:
        outcome = QuantityOutcome.NOT_CLAIMED
    else:
        outcome = QuantityOutcome.REALIZED if realized else QuantityOutcome.NOT_REALIZED
    return QuantityEstimate(
        **values,
        delta_per_hour=delta,
        annualized=annualized,
        claimed_annualized=annualized if claimed and realized else 0.0,
        outcome=outcome,
        projection_accuracy=annualized / projected if projected else None,
    )


def _projection_notes(estimates: Sequence[QuantityEstimate], min_ratio: float) -> List[str]:
    """Report (never gate on) a measured saving far below its projection."""
    notes = []
    for estimate in estimates:
        if (
            estimate.outcome is QuantityOutcome.REALIZED
            and estimate.projection_accuracy is not None
            and estimate.projection_accuracy < min_ratio
        ):
            notes.append(
                f"measured {estimate.quantity} saving is {estimate.projection_accuracy:.0%} of the projection "
                f"(below {min_ratio:.0%}; reported, not a gate)"
            )
    return notes


def _not_realized_reason(estimate: QuantityEstimate) -> str:
    annualized = estimate.annualized or 0.0
    if annualized <= 0:
        return f"measured {estimate.quantity} change is {annualized:+.2f}/yr (no saving)"
    return (
        f"measured {estimate.quantity} saving {annualized:.2f}/yr is within the "
        f"{MIN_RELATIVE_CHANGE:.0%} noise floor of the baseline rate"
    )


def _find(estimates: Sequence[QuantityEstimate], quantity: str) -> Optional[QuantityEstimate]:
    return next((estimate for estimate in estimates if estimate.quantity == quantity), None)


def _measured_value(estimate: Optional[QuantityEstimate], outcome: MeasurementOutcome) -> Optional[float]:
    """Value stored on the recommendation: the claimed saving, never negative.

    Signed values stay in the immutable measurement; a quantity that is not
    claimed (CO2e of a rightsizing) is stored as 0 so nothing is credited.
    """
    if estimate is None or outcome is MeasurementOutcome.INCONCLUSIVE:
        return None
    if estimate.outcome is QuantityOutcome.NOT_CLAIMED:
        return 0.0
    if estimate.outcome is QuantityOutcome.REALIZED:
        return estimate.claimed_annualized
    if estimate.annualized is None:
        return None
    return max(estimate.annualized, 0.0)


def _series_for_record(record: RecommendationRecord, metrics: Sequence) -> List:
    """Filters metrics down to the recommendation target."""
    from greenkube.core.optimization.targeting import group_by_recommendation_target

    def for_container(series: List) -> List:
        if record.container_name is None:
            return series
        if not record.container_name or record.container_name.strip() != record.container_name:
            return []
        return [metric for metric in series if metric.container_name == record.container_name]

    grouped = group_by_recommendation_target(list(metrics))
    if record.owner_kind and record.owner_name:
        series = grouped.get((record.namespace or "", record.owner_kind, record.owner_name))
        if series:
            return for_container(series)
    if record.pod_name:
        for key, series in grouped.items():
            if key[0] == record.namespace and key[2] == record.pod_name:
                return for_container(series)
    return []
