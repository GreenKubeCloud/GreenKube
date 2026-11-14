# src/greenkube/verify/model.py
"""Domain objects of change measurement (roadmap C1, C2, C4, C5).

A :class:`ChangeEvent` is one change that landed on one workload (a GitOps
plan merged and synced, or a change detected in the cluster). It is immutable
and created by the applied detector together with a :class:`BaselineSnapshot`
of the workload before the change. Each change is measured as a whole
(:class:`ChangeMeasurement`, C5: one measurement per workload change) and its
lifecycle is tracked by :class:`ChangeStatus` (C2).

Every object here is a value object: the storage layer persists them as
insert-only rows except :class:`ChangeStatus`, whose transitions are
compare-and-swap writes recorded with a :class:`ChangeStateEvent`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from greenkube.models.verification import HealthGateResult

#: Version of the change-measurement record format (bumped on breaking changes).
CHANGE_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for every content digest of this package."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(value: Any) -> str:
    """SHA-256 of the canonical JSON of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class WorkloadRef(_Frozen):
    """The workload a change applies to."""

    namespace: str
    kind: str
    name: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.namespace, self.kind, self.name)


class TimeWindow(_Frozen):
    """A half-open UTC time window ``[start, end)``."""

    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def hours(self) -> float:
        return self.duration.total_seconds() / 3600.0

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


class ChangeApplicationMethod(str, Enum):
    """How a change reached the cluster."""

    GITOPS_PR = "gitops_pr"
    MANUAL = "manual"
    DETECTED = "detected"


class ChangeEvent(_Frozen):
    """One applied change on one workload (immutable, C1).

    ``spec_digest_*`` are SHA-256 digests of the normalized pod template;
    ``spec_digest_before`` and ``generation_before`` are ``None`` when the
    detector never observed the workload before the change. ``baseline_window_end``
    is the PR merge (or plan approval) time; the baseline never extends past it.
    ``recommendation_ids`` lists the members known when the change was read
    (members detected in a later pass are appended, never removed).
    """

    change_id: str
    recommendation_ids: List[int] = Field(default_factory=list)
    plan_id: Optional[str] = None
    pr_url: Optional[str] = None
    merge_sha: Optional[str] = None
    gitops_revision: Optional[str] = None
    synced_at: Optional[datetime] = None
    target: WorkloadRef
    containers: List[str] = Field(default_factory=list)
    workload_uid: Optional[str] = None
    generation_before: Optional[int] = None
    generation_after: Optional[int] = None
    spec_digest_before: Optional[str] = None
    spec_digest_after: Optional[str] = None
    rollout_started_at: datetime
    rollout_started_source: str = "unknown"
    rollout_completed_at: datetime
    rollout_completed_source: str = "unknown"
    baseline_window_end: datetime
    action_kind: str = "container_resources"
    application_method: ChangeApplicationMethod = ChangeApplicationMethod.DETECTED
    load_metric: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "applied-detector"


class HourBucket(_Frozen):
    """Aggregated observations of one workload over one UTC clock hour.

    Rates are per hour of covered time (so a partially covered hour is not
    biased); ``coverage`` is the share of the hour with observations, and
    ``cost_coverage``/``co2e_coverage`` the share with trusted cost or carbon.
    ``replicas`` is the average pod count; ``core_hours``/``gib_hours`` are the
    requested CPU cores and memory GiB summed over pods (per hour).
    ``embodied_co2e_g``/``embodied_coverage`` are ``None`` when the bucket was
    aggregated before embodied emissions were measured (older snapshots).
    """

    hour: datetime
    coverage: float = 0.0
    replicas: Optional[float] = None
    core_hours: Optional[float] = None
    gib_hours: Optional[float] = None
    cpu_usage_cores: Optional[float] = None
    cost: Optional[float] = None
    cost_cpu: Optional[float] = None
    cost_ram: Optional[float] = None
    cost_coverage: float = 0.0
    energy_kwh: Optional[float] = None
    co2e_g: Optional[float] = None
    co2e_coverage: float = 0.0
    #: Embodied (hardware manufacturing) CO2e allocated to the workload, per hour.
    #: ``None`` in snapshots frozen before embodied emissions were measured.
    embodied_co2e_g: Optional[float] = None
    embodied_coverage: Optional[float] = None
    instance_types: dict[str, float] = Field(default_factory=dict)

    @property
    def hour_of_week(self) -> int:
        return hour_of_week(self.hour)


#: HourBucket fields added after snapshots were first frozen (omitted from the digest while unset).
_OPTIONAL_BUCKET_FIELDS = ("embodied_co2e_g", "embodied_coverage")


def hour_of_week(moment: datetime) -> int:
    """Index 0..167 of the UTC hour within the week (Monday 00:00 = 0)."""
    return moment.weekday() * 24 + moment.hour


class BaselineSnapshot(_Frozen):
    """Frozen pre-change observations of a workload (immutable, C1).

    ``hours`` covers ``window`` (≥ 7 days ending at the merge or approval);
    ``load_series`` holds the candidate load drivers' hourly values over the
    same window, keyed by driver then ISO hour. ``content_sha256`` covers every
    other field, so the counterfactual of a measurement can be re-derived and
    audited.
    """

    snapshot_id: str
    change_id: str
    content_sha256: str = ""
    window: TimeWindow
    hours: List[HourBucket] = Field(default_factory=list)
    load_series: dict[str, dict[str, float]] = Field(default_factory=dict)
    price_card: dict[str, Any] = Field(default_factory=dict)
    intensity_source: Optional[str] = None
    impact_model_version: Optional[str] = None
    collector_versions: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def digest_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude={"content_sha256", "created_at"})
        for bucket in payload.get("hours") or []:
            # Fields added after the first snapshots were frozen are left out while unset,
            # so the digest of an older snapshot still verifies.
            for key in _OPTIONAL_BUCKET_FIELDS:
                if bucket.get(key) is None:
                    bucket.pop(key, None)
        return payload

    def with_digest(self) -> "BaselineSnapshot":
        return self.model_copy(update={"content_sha256": sha256_hex(self.digest_payload())})

    def verify_digest(self) -> bool:
        return bool(self.content_sha256) and self.content_sha256 == sha256_hex(self.digest_payload())


class WindowPlan(_Frozen):
    """Pre- and post-change windows of one measurement (C3)."""

    pre: TimeWindow
    post: TimeWindow
    warmup_hours: float
    min_coverage: float


class MeasurementState(str, Enum):
    """Lifecycle of a change measurement (C2)."""

    APPLIED = "applied"
    MEASURING = "measuring"
    REALIZED = "realized"
    NOT_REALIZED = "not_realized"
    INCONCLUSIVE = "inconclusive"
    CONTAMINATED = "contaminated"
    REGRESSED = "regressed"
    CLOSED = "closed"


#: Decided outcomes of one measurement.
DECIDED_STATES = frozenset(
    {
        MeasurementState.REALIZED,
        MeasurementState.NOT_REALIZED,
        MeasurementState.INCONCLUSIVE,
        MeasurementState.CONTAMINATED,
    }
)

#: Allowed transitions. A new measurement of a change that was not realized,
#: inconclusive or contaminated is only started by an explicit re-measurement
#: (C0.5); a realized saving ends regressed (C8) or closed (superseded).
ALLOWED_TRANSITIONS: dict[MeasurementState, frozenset[MeasurementState]] = {
    MeasurementState.APPLIED: frozenset({MeasurementState.MEASURING, MeasurementState.CLOSED}),
    MeasurementState.MEASURING: frozenset(DECIDED_STATES | {MeasurementState.CLOSED}),
    MeasurementState.REALIZED: frozenset({MeasurementState.REGRESSED, MeasurementState.CLOSED}),
    MeasurementState.NOT_REALIZED: frozenset({MeasurementState.MEASURING, MeasurementState.CLOSED}),
    MeasurementState.INCONCLUSIVE: frozenset({MeasurementState.MEASURING, MeasurementState.CLOSED}),
    MeasurementState.CONTAMINATED: frozenset({MeasurementState.MEASURING, MeasurementState.CLOSED}),
    MeasurementState.REGRESSED: frozenset({MeasurementState.CLOSED}),
    MeasurementState.CLOSED: frozenset(),
}


def is_allowed_transition(current: MeasurementState, target: MeasurementState) -> bool:
    """Whether ``current → target`` is a lifecycle transition.

    ``measuring → measuring`` records an event (e.g. ``health_checked``) on a
    change that is still being measured; ``closed → closed`` ends the measured
    accrual of a superseded change (C8): it was closed when a newer change
    landed on its workload, and keeps accruing its own step until that newer
    change's saving ends or the workload is deleted or drifts.
    """
    if current is target and current in (MeasurementState.MEASURING, MeasurementState.CLOSED):
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class ChangeOutcome(str, Enum):
    """Outcome of one measurement (C4)."""

    REALIZED = "realized"
    NOT_REALIZED = "not_realized"
    INCONCLUSIVE = "inconclusive"
    CONTAMINATED = "contaminated"


Severity = Literal["mild", "moderate", "severe"]


class Confounder(_Frozen):
    """Something other than the change that may explain part of the delta (C4)."""

    kind: str
    severity: Severity
    detail: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class LoadDriverInfo(_Frozen):
    """The load driver used to normalize the counterfactual (C3)."""

    key: str
    label: str
    coverage_pre: float
    coverage_post: float
    weak: bool = False
    detail: str = ""


class SavingsEstimate(_Frozen):
    """Measured saving of one quantity (signed: positive is a saving).

    ``rate_per_hour`` is the mean hourly saving over the aligned post-window
    hours; ``ci_low``/``ci_high`` its 90% moving-block bootstrap interval.
    Values are never floored at zero. ``projection_accuracy`` (measured ÷
    projected, annual) is reported only and never used as a gate.
    """

    quantity: str
    unit: str
    claimed: bool
    rate_per_hour: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    counterfactual_per_hour: Optional[float] = None
    actual_per_hour: Optional[float] = None
    annualized: Optional[float] = None
    annualized_ci_low: Optional[float] = None
    annualized_ci_high: Optional[float] = None
    coverage: float = 0.0
    aligned_hours: int = 0
    elasticity: float = 0.0
    #: Counterfactual model used (``ols_load_seasonal`` or ``hour_of_week_mean``; None before C3 v2).
    method: Optional[str] = None
    #: Impact components the measured quantity covers (CO2e only, for component-matched comparison).
    components: List[str] = Field(default_factory=list)
    outcome: ChangeOutcome
    reason: str = ""
    projected_annual: Optional[float] = None
    projection_accuracy: Optional[float] = None
    #: Where a frozen rate came from (deletions: ``observed``, ``opencost``, ``custom_rate``...); None otherwise.
    rate_source: Optional[str] = None


class Allocation(_Frozen):
    """Share of a change measurement credited to one recommendation (C5)."""

    recommendation_id: int
    basis: str
    share: float
    quantity: str
    annualized: Optional[float] = None
    measurement_id: Optional[str] = None


class ChangeMeasurement(_Frozen):
    """One decided measurement of a change (immutable).

    ``supersedes_measurement_id`` links a re-measurement to the measurement it
    replaces. ``evidence_sha256`` covers every decided field.
    """

    measurement_id: str
    change_id: str
    sequence: int = 1
    strategy_key: str
    strategy_version: str
    impact_model_version: Optional[str] = None
    baseline_snapshot_id: Optional[str] = None
    baseline_sha256: Optional[str] = None
    windows: WindowPlan
    load_driver: Optional[LoadDriverInfo] = None
    counterfactual_params: dict[str, Any] = Field(default_factory=dict)
    estimates: List[SavingsEstimate] = Field(default_factory=list)
    confounders: List[Confounder] = Field(default_factory=list)
    confidence: float = 0.0
    health: List[HealthGateResult] = Field(default_factory=list)
    allocations: List[Allocation] = Field(default_factory=list)
    outcome: ChangeOutcome
    reasons: List[str] = Field(default_factory=list)
    evidence_sha256: str = ""
    supersedes_measurement_id: Optional[str] = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def estimate(self, quantity: str) -> Optional[SavingsEstimate]:
        return next((item for item in self.estimates if item.quantity == quantity), None)

    def with_evidence_digest(self) -> "ChangeMeasurement":
        payload = self.model_dump(mode="json", exclude={"evidence_sha256"})
        return self.model_copy(update={"evidence_sha256": sha256_hex(payload)})


class ChangeStatus(BaseModel):
    """Current lifecycle state of a change (mutable through compare-and-swap)."""

    change_id: str
    state: MeasurementState
    version: int = 1
    sequence: int = 1
    latest_measurement_id: Optional[str] = None
    post_window_start: Optional[datetime] = None
    post_window_end: Optional[datetime] = None
    details: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChangeStateEvent(BaseModel):
    """One audit event of a change's lifecycle (append-only)."""

    id: Optional[int] = None
    change_id: str
    event_type: str
    from_state: Optional[MeasurementState] = None
    to_state: Optional[MeasurementState] = None
    actor: str = "system"
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkloadSpecObservation(BaseModel):
    """A pod-template revision of a workload, as first seen by the applied detector."""

    namespace: str
    kind: str
    name: str
    workload_uid: Optional[str] = None
    generation: Optional[int] = None
    spec_digest: Optional[str] = None
    hpa_digest: Optional[str] = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.namespace, self.kind, self.name)

    def same_revision(self, other: "WorkloadSpecObservation") -> bool:
        return (
            self.workload_uid == other.workload_uid
            and self.generation == other.generation
            and self.spec_digest == other.spec_digest
            and self.hpa_digest == other.hpa_digest
        )
