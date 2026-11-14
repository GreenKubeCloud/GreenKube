"""Fail-closed policy evaluation for GitOps automation plans."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal, Protocol, cast

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from greenkube.automation.actions import operation_kind_of
from greenkube.gitops.contracts import (
    AddHorizontalPodAutoscaler,
    AutomationErrorCode,
    ControllerKind,
    Plan,
    PlanStatus,
    WorkloadSource,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationType, RiskLevel
from greenkube.storage.base_gitops_repository import AutomationPlanRecord
from greenkube.utils.time import ensure_utc

_RISK_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
_ACTIVE_PLAN_STATUSES = frozenset({PlanStatus.DRAFT, PlanStatus.VALIDATED, PlanStatus.APPROVED, PlanStatus.APPLYING})
_COOLDOWN_STATUSES = frozenset(
    {
        PlanStatus.PR_OPEN,
        PlanStatus.MERGED,
        PlanStatus.SYNCED,
        PlanStatus.APPLIED,
        PlanStatus.SYNC_FAILED,
        PlanStatus.CLOSED,
    }
)
_HISTORY_STATUSES = tuple(sorted(_ACTIVE_PLAN_STATUSES | _COOLDOWN_STATUSES, key=lambda status: status.value))
# VPA update modes in which the VPA itself writes pod requests: a conflicting writer (D13).
_CONFLICTING_VPA_UPDATE_MODES = frozenset({"Auto", "Recreate", "InPlace", "InPlaceOrRecreate"})
# HPA metric targets that do not depend on the pod requests (absolute usage), D13.
_REQUEST_INDEPENDENT_HPA_TARGETS = frozenset({"AverageValue", "Value"})
MissingEvidenceMode = Literal["fail", "warn"]


# --------------------------------------------------------------------------- evidence warm-up (F7)
#
# Evidence gates are computed relative to the history that exists. Right after
# install, the observation window (the lookback, e.g. 7 days) mostly predates the
# first collected sample, so the coverage of the whole window stays low for days
# even though every step since install was collected. ``minCoverage`` is therefore
# evaluated against the data available since the first sample, capped at the
# lookback, and ``eligible_at = first_sample + required history`` where the
# required history is ``minSamples`` query steps.

#: How long the first collected sample (``earliest_input_time``) is cached per process.
HISTORY_START_TTL_SECONDS = 600.0
_history_cache: dict[str, Any] = {"value": None, "read_at": None}


def known_history_start() -> datetime | None:
    """The cached first-sample time of this installation (None until read or when unknown)."""
    value = _history_cache.get("value")
    return value if isinstance(value, datetime) else None


def set_history_start(value: datetime | None, *, read_at: float | None = None) -> None:
    import time

    _history_cache["value"] = ensure_utc(value) if isinstance(value, datetime) else None
    _history_cache["read_at"] = time.monotonic() if read_at is None else read_at


async def collection_history_start() -> datetime | None:
    """First collected sample of this installation (cached); None when unknown.

    Read from the workload usage store (``earliest_input_time``). A failure keeps
    the last known value: without one, the gates fall back to the full lookback.
    """
    import time

    read_at = _history_cache.get("read_at")
    if isinstance(read_at, float) and time.monotonic() - read_at < HISTORY_START_TTL_SECONDS:
        return known_history_start()
    try:
        from greenkube.core.config import get_config
        from greenkube.core.db import get_db_manager
        from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository

        value = await create_workload_usage_repository(get_db_manager(), get_config().DB_TYPE).earliest_input_time()
    except Exception:
        return known_history_start()
    set_history_start(value)
    return known_history_start()


def _evidence_window(evidence: Any) -> tuple[float | None, datetime | None, datetime | None]:
    window = getattr(evidence, "observation_window_seconds", None)
    valid = isinstance(window, (int, float)) and not isinstance(window, bool) and math.isfinite(window) and window > 0
    window_seconds = float(window) if valid else None
    end = getattr(evidence, "observation_window_end", None)
    start = getattr(evidence, "observation_window_start", None)
    end = ensure_utc(end) if isinstance(end, datetime) else None
    start = ensure_utc(start) if isinstance(start, datetime) else None
    if start is None and end is not None and window_seconds is not None:
        start = end - timedelta(seconds=window_seconds)
    return window_seconds, start, end


def _usable_history_start(history_start: datetime | None, end: datetime | None) -> datetime | None:
    """The install's first sample, or None when it cannot apply to this evidence.

    A first sample at or after the evidence window end (stale persisted evidence,
    or retention moving the earliest stored sample forward) says nothing about
    the history behind this evidence: treat it as unknown (full lookback).
    """
    if history_start is None:
        return None
    history_start = ensure_utc(history_start)
    if end is not None and history_start >= end:
        return None
    return history_start


def available_history_seconds(evidence: Any, history_start: datetime | None, step_seconds: int = 300) -> float | None:
    """Seconds of the observation window covered by data since install, capped at the lookback."""
    window, start, end = _evidence_window(evidence)
    if window is None:
        return None
    history_start = _usable_history_start(history_start, end)
    if history_start is None or end is None:
        return window
    first = max(start, ensure_utc(history_start)) if start is not None else ensure_utc(history_start)
    available = (end - first).total_seconds()
    return min(window, max(available, float(max(step_seconds, 1))))


def coverage_since_history(evidence: Any, history_start: datetime | None, step_seconds: int = 300) -> float:
    """Coverage of the data available since install (the stored coverage is over the whole lookback)."""
    coverage = getattr(evidence, "coverage_ratio", 0.0)
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(coverage):
        return 0.0
    window, _, _ = _evidence_window(evidence)
    available = available_history_seconds(evidence, history_start, step_seconds)
    if window is None or available is None or available <= 0:
        return float(coverage)
    return max(0.0, min(1.0, float(coverage) * window / available))


def evidence_eligible_at(
    evidence: Any,
    *,
    min_samples: int,
    min_coverage: float,
    step_seconds: int,
    history_start: datetime | None,
) -> datetime | None:
    """When the evidence gates pass: ``first_sample + minSamples x step`` (F7).

    The first sample is the later of the window start and the install's first
    collected sample; without the latter it is estimated from the sample count
    (one sample per step). When the coverage since install is below
    ``minCoverage`` (collection gaps), the missing share of the available history
    is added after the window end.
    """
    window, start, end = _evidence_window(evidence)
    if end is None:
        return None
    step = max(int(step_seconds or 300), 1)
    coverage = getattr(evidence, "coverage_ratio", 0.0)
    coverage = float(coverage) if isinstance(coverage, (int, float)) and not isinstance(coverage, bool) else 0.0
    history_start = _usable_history_start(history_start, end)
    if history_start is not None:
        first = max(start, ensure_utc(history_start)) if start is not None else ensure_utc(history_start)
    else:
        # Unknown install time: estimate the first sample from the samples collected so far.
        samples = int(getattr(evidence, "sample_count", 0) or 0)
        first = end - timedelta(seconds=samples * step)
    eligible_at = first + timedelta(seconds=min_samples * step)
    # A workload younger than the install (or than the window) has fewer samples than
    # the steps since the first sample: the missing samples still accrue one per step.
    samples = int(getattr(evidence, "sample_count", 0) or 0)
    if samples < min_samples:
        eligible_at = max(eligible_at, end + timedelta(seconds=(min_samples - samples) * step))
    since = coverage_since_history(evidence, history_start, step)
    if since < min_coverage:
        available = available_history_seconds(evidence, history_start, step) or 0.0
        eligible_at = max(eligible_at, end + timedelta(seconds=(min_coverage - since) * available))
    return eligible_at


class ChangeFreezeWindow(BaseModel):
    """An absolute, timezone-aware change-freeze interval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: datetime
    end: datetime
    reason: str | None = None

    @field_validator("start", "end")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("change-freeze window timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_interval(self) -> ChangeFreezeWindow:
        if self.end <= self.start:
            raise ValueError("change-freeze window end must be after start")
        return self


class AutomationPolicyConfig(BaseModel):
    """Documented GitOps policy defaults plus optional freeze schedules."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    max_risk_level: RiskLevel = Field(
        default=RiskLevel.MEDIUM,
        validation_alias=AliasChoices("maxRiskLevel", "max_risk_level"),
    )
    min_confidence: float = Field(
        default=0.6,
        ge=0,
        le=1,
        validation_alias=AliasChoices("minConfidence", "min_confidence"),
    )
    min_samples: int = Field(
        default=36,
        ge=1,
        validation_alias=AliasChoices("minSamples", "min_samples"),
    )
    min_coverage: float = Field(
        default=0.5,
        ge=0,
        le=1,
        validation_alias=AliasChoices("minCoverage", "min_coverage"),
    )
    max_open_pull_requests_per_workload: int = Field(
        default=1,
        ge=0,
        validation_alias=AliasChoices(
            "maxOpenPullRequestsPerWorkload",
            "max_open_pull_requests_per_workload",
        ),
    )
    cooldown_minutes: int = Field(
        default=60,
        ge=0,
        validation_alias=AliasChoices("cooldownMinutes", "cooldown_minutes"),
    )
    change_freeze_labels: tuple[str, ...] = Field(
        default=(),
        validation_alias=AliasChoices("changeFreezeLabels", "change_freeze_labels"),
    )
    change_freeze_windows: tuple[ChangeFreezeWindow, ...] = Field(
        default=(),
        validation_alias=AliasChoices("changeFreezeWindows", "change_freeze_windows"),
    )
    # What a live gate does when discovery could not collect its evidence (rollout,
    # controller health, HPA/VPA/PDB): "fail" rejects, "warn" allows with a warning (D13).
    # One install, no profiles (F11): fail closed by default (principle 6).
    missing_evidence: MissingEvidenceMode = Field(
        default="fail",
        validation_alias=AliasChoices("missingEvidence", "missing_evidence"),
    )

    @field_validator("change_freeze_labels")
    @classmethod
    def validate_freeze_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            if not value or value != value.strip() or value.startswith("=") or value.endswith("="):
                raise ValueError("change-freeze labels must be non-blank label keys or key=value selectors")
            if value.count("=") > 1:
                raise ValueError("change-freeze label selectors may contain at most one '='")
            if value not in normalized:
                normalized.append(value)
        return tuple(normalized)

    @classmethod
    def from_json(cls, value: str | None) -> AutomationPolicyConfig:
        if value is None or not value.strip():
            return cls()
        if len(value.encode("utf-8")) > 16_384:
            raise ValueError("GITOPS_POLICY_JSON exceeds the supported configuration size")
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("GITOPS_POLICY_JSON must contain a valid JSON object") from None
        if not isinstance(payload, Mapping):
            raise ValueError("GITOPS_POLICY_JSON must contain a JSON object")
        return cls.model_validate(payload)

    def public_values(self) -> dict[str, Any]:
        return {
            "maxRiskLevel": self.max_risk_level.value,
            "minConfidence": self.min_confidence,
            "minSamples": self.min_samples,
            "minCoverage": self.min_coverage,
            "maxOpenPullRequestsPerWorkload": self.max_open_pull_requests_per_workload,
            "cooldownMinutes": self.cooldown_minutes,
            "changeFreezeLabels": list(self.change_freeze_labels),
            "changeFreezeWindows": [window.model_dump(mode="json") for window in self.change_freeze_windows],
            "missingEvidence": self.missing_evidence,
        }


class PolicyCheckState(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"
    NOT_CONFIGURED = "not_configured"
    NOT_AVAILABLE = "not_available"


class PolicyCheck(BaseModel):
    """One explainable gate evaluated against current evidence."""

    model_config = ConfigDict(extra="forbid")

    gate: str
    state: PolicyCheckState
    message: str
    observed: Any = None
    limit: Any = None
    code: str | None = None


class PolicyRejection(BaseModel):
    """Actionable, typed reason that prevents a plan operation."""

    model_config = ConfigDict(extra="forbid")

    code: AutomationErrorCode
    detail: str


class AutomationPolicyResult(BaseModel):
    """Versioned policy decision stored with an automation plan."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    allowed: bool
    evaluated_at: datetime
    workload: dict[str, str]
    policy: dict[str, Any]
    checks: list[PolicyCheck]
    rejections: list[PolicyRejection]
    # Non-blocking findings, e.g. missing live evidence under missingEvidence=warn or an
    # HPA scaling on a resource the plan does not change.
    warnings: list[PolicyRejection] = Field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class PolicyPlanRepository(Protocol):
    """Plan-history evidence used by workload budgets and cooldowns."""

    async def list_plans(
        self,
        *,
        status: PlanStatus | None = None,
        source_id: str | None = None,
    ) -> list[Plan]: ...

    async def get_plan_record(self, plan_id: str) -> AutomationPlanRecord | None: ...


class AutomationPolicyEvaluator:
    """Evaluate policy using persisted recommendation, binding and plan evidence."""

    def __init__(
        self,
        *,
        config: AutomationPolicyConfig,
        plan_repository: PolicyPlanRepository | None,
        clock=lambda: datetime.now(timezone.utc),
        history_start=None,
        step_seconds: int | None = None,
        controller_status_reader: ControllerStatusReader | None = None,
    ) -> None:
        self.config = config
        self.plan_repository = plan_repository
        self.clock = clock
        #: Reads the owning controller's live status (Argo CD ``Application.status``) when its
        #: binding evidence reports it unhealthy, to tell a self-caused health problem apart.
        self.controller_status_reader = controller_status_reader
        #: First collected sample of the installation (F7): an async callable, a datetime, or
        #: None for the process-wide cached reader.
        self.history_start = history_start
        self.step_seconds = step_seconds

    async def _history_start(self) -> datetime | None:
        source = self.history_start
        if isinstance(source, datetime):
            return ensure_utc(source)
        try:
            reader = cast(Callable[[], Awaitable[Any]], source) if callable(source) else collection_history_start
            value = await reader()
        except Exception:
            return None
        return ensure_utc(value) if isinstance(value, datetime) else None

    def _step_seconds(self) -> int:
        if self.step_seconds:
            return int(self.step_seconds)
        try:
            from greenkube.core.config import get_config
            from greenkube.utils.time import parse_query_step_seconds

            return parse_query_step_seconds(str(get_config().PROMETHEUS_QUERY_RANGE_STEP or "5m"))
        except Exception:
            return 300

    async def evaluate(
        self,
        records: Sequence[RecommendationRecord],
        source: WorkloadSource,
        *,
        exclude_plan_id: str | None = None,
        exclude_plan_ids: Sequence[str] = (),
    ) -> AutomationPolicyResult:
        now = ensure_utc(self.clock())
        workload = {
            "cluster": source.cluster,
            "namespace": source.namespace,
            "kind": source.workload_kind,
            "name": source.workload_name,
        }
        checks: list[PolicyCheck] = []
        rejections: list[PolicyRejection] = []
        warnings: list[PolicyRejection] = []

        def add_check(
            gate: str,
            state: PolicyCheckState,
            message: str,
            *,
            observed: Any = None,
            limit: Any = None,
            code: AutomationErrorCode | None = None,
            detail: str | None = None,
        ) -> None:
            checks.append(
                PolicyCheck(
                    gate=gate,
                    state=state,
                    message=message,
                    observed=observed,
                    limit=limit,
                    code=code.value if code is not None else None,
                )
            )
            if state is PolicyCheckState.REJECTED and code is not None:
                rejection_detail = detail or message
                rejection = PolicyRejection(code=code, detail=rejection_detail)
                if rejection not in rejections:
                    rejections.append(rejection)

        risk_values = [_record_risk(record) for record in records]
        missing_risk_ids = [record.id for record, risk in zip(records, risk_values, strict=True) if risk is None]
        if not records or missing_risk_ids:
            add_check(
                "maximum_risk",
                PolicyCheckState.REJECTED,
                "Risk evidence is missing for one or more recommendations.",
                observed={"missing_recommendation_ids": missing_risk_ids},
                limit=self.config.max_risk_level.value,
                code=AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                detail=("Refresh the affected recommendations so each has a risk level before planning or approval."),
            )
        else:
            highest_risk = max((risk for risk in risk_values if risk is not None), key=_RISK_RANK.__getitem__)
            if _RISK_RANK[highest_risk] > _RISK_RANK[self.config.max_risk_level]:
                add_check(
                    "maximum_risk",
                    PolicyCheckState.REJECTED,
                    "Recommendation risk exceeds the configured maximum.",
                    observed=highest_risk.value,
                    limit=self.config.max_risk_level.value,
                    code=AutomationErrorCode.POLICY_RISK_EXCEEDED,
                    detail=(
                        f"Risk {highest_risk.value!r} exceeds the configured maximum "
                        f"{self.config.max_risk_level.value!r}; lower the risk or adjust policy."
                    ),
                )
            else:
                add_check(
                    "maximum_risk",
                    PolicyCheckState.PASSED,
                    "Recommendation risk is within the configured maximum.",
                    observed=highest_risk.value,
                    limit=self.config.max_risk_level.value,
                )

        confidence_values = [_record_confidence(record) for record in records]
        missing_confidence_ids = [
            record.id for record, confidence in zip(records, confidence_values, strict=True) if confidence is None
        ]
        if not records or missing_confidence_ids:
            add_check(
                "minimum_confidence",
                PolicyCheckState.REJECTED,
                "Confidence evidence is missing for one or more recommendations.",
                observed={"missing_recommendation_ids": missing_confidence_ids},
                limit=self.config.min_confidence,
                code=AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                detail=(
                    "Refresh the affected recommendations so each has a confidence score before planning or approval."
                ),
            )
        else:
            lowest_confidence = min(value for value in confidence_values if value is not None)
            if lowest_confidence < self.config.min_confidence:
                add_check(
                    "minimum_confidence",
                    PolicyCheckState.REJECTED,
                    "Recommendation confidence is below the configured minimum.",
                    observed=lowest_confidence,
                    limit=self.config.min_confidence,
                    code=AutomationErrorCode.POLICY_CONFIDENCE_TOO_LOW,
                    detail=(
                        f"Confidence {lowest_confidence:.3f} is below the configured minimum "
                        f"{self.config.min_confidence:.3f}; collect stronger evidence before retrying."
                    ),
                )
            else:
                add_check(
                    "minimum_confidence",
                    PolicyCheckState.PASSED,
                    "Recommendation confidence meets the configured minimum.",
                    observed=lowest_confidence,
                    limit=self.config.min_confidence,
                )

        self._evaluate_freeze_windows(now, add_check)
        self._evaluate_freeze_labels(source, add_check)

        def add_missing_evidence(gate: str, message: str, detail: str) -> None:
            if self.config.missing_evidence == "fail":
                add_check(
                    gate,
                    PolicyCheckState.REJECTED,
                    message,
                    code=AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                    detail=detail,
                )
                return
            add_check(gate, PolicyCheckState.NOT_AVAILABLE, f"{message} Allowed by missingEvidence=warn.")
            add_warning(AutomationErrorCode.POLICY_EVIDENCE_MISSING, detail)

        def add_warning(code: AutomationErrorCode, detail: str) -> None:
            warning = PolicyRejection(code=code, detail=detail)
            if warning not in warnings:
                warnings.append(warning)

        self._evaluate_optional_live_gates(
            source,
            records,
            add_check,
            add_missing_evidence,
            add_warning,
            history_start=await self._history_start(),
            step_seconds=self._step_seconds(),
            controller_health_exemption=await self._controller_health_exemption(source, records),
        )

        if self.plan_repository is None:
            self._reject_missing_history(add_check)
        else:
            # One indexed, batched read returns plans with their ``updated_at`` so cooldowns
            # need no per-plan lookup (D15, BUG-57).
            updated_at_by_plan: dict[str, datetime | None] | None = None
            try:
                identity = {
                    "cluster": source.cluster,
                    "namespace": source.namespace,
                    "workload_kind": source.workload_kind,
                    "workload_name": source.workload_name,
                    "statuses": _HISTORY_STATUSES,
                }
                workload_record_reader = getattr(self.plan_repository, "list_plan_records_for_workload", None)
                workload_history_reader = getattr(self.plan_repository, "list_plans_for_workload", None)
                if callable(workload_record_reader):
                    history_records = list(await cast(Any, workload_record_reader)(**identity))
                    plans = [record.plan for record in history_records]
                    updated_at_by_plan = {record.plan.plan_id: record.updated_at for record in history_records}
                elif callable(workload_history_reader):
                    plans = await cast(Any, workload_history_reader)(**identity)
                else:
                    plans = await self.plan_repository.list_plans()
            except Exception:
                plans = None
            if plans is None:
                self._reject_missing_history(add_check)
            else:
                excluded_plan_ids = set(exclude_plan_ids)
                if exclude_plan_id is not None:
                    excluded_plan_ids.add(exclude_plan_id)
                workload_plans = [
                    plan
                    for plan in plans
                    if _same_workload(plan.source, source) and plan.plan_id not in excluded_plan_ids
                ]
                open_pull_request_count = sum(plan.status is PlanStatus.PR_OPEN for plan in workload_plans)
                if open_pull_request_count >= self.config.max_open_pull_requests_per_workload:
                    add_check(
                        "open_pull_request_budget",
                        PolicyCheckState.REJECTED,
                        "The workload has reached its open pull-request limit.",
                        observed=open_pull_request_count,
                        limit=self.config.max_open_pull_requests_per_workload,
                        code=AutomationErrorCode.WORKLOAD_PR_LIMIT_EXCEEDED,
                        detail=(
                            f"This workload has {open_pull_request_count} open pull request(s), at the configured "
                            f"limit of {self.config.max_open_pull_requests_per_workload}; close or merge one "
                            "before retrying."
                        ),
                    )
                else:
                    add_check(
                        "open_pull_request_budget",
                        PolicyCheckState.PASSED,
                        "The workload is below its open pull-request limit.",
                        observed=open_pull_request_count,
                        limit=self.config.max_open_pull_requests_per_workload,
                    )

                active_plans = [plan for plan in workload_plans if plan.status in _ACTIVE_PLAN_STATUSES]
                if active_plans:
                    add_check(
                        "active_plan",
                        PolicyCheckState.REJECTED,
                        "Another plan for this workload is still active.",
                        observed=len(active_plans),
                        limit=0,
                        code=AutomationErrorCode.WORKLOAD_PLAN_ALREADY_EXISTS,
                        detail=(
                            "Resolve or cancel the existing workload plan before creating or approving another one."
                        ),
                    )
                else:
                    add_check(
                        "active_plan",
                        PolicyCheckState.PASSED,
                        "No other active plan exists for this workload.",
                        observed=0,
                        limit=0,
                    )

                await self._evaluate_cooldown(
                    workload_plans,
                    now,
                    add_check,
                    updated_at_by_plan=updated_at_by_plan,
                )

        return AutomationPolicyResult(
            allowed=not rejections,
            evaluated_at=now,
            workload=workload,
            policy=self.config.public_values(),
            checks=checks,
            rejections=rejections,
            warnings=warnings,
        )

    def _evaluate_freeze_windows(self, now: datetime, add_check) -> None:
        active = [window for window in self.config.change_freeze_windows if window.start <= now < window.end]
        if active:
            window = active[0]
            add_check(
                "change_freeze_window",
                PolicyCheckState.REJECTED,
                "A configured change-freeze window is active.",
                observed={
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "reason": window.reason,
                },
                code=AutomationErrorCode.CHANGE_FREEZE_ACTIVE,
                detail=(
                    f"A configured change-freeze window is active until {window.end.isoformat()}; retry after it ends."
                ),
            )
        elif self.config.change_freeze_windows:
            add_check(
                "change_freeze_window",
                PolicyCheckState.PASSED,
                "No configured change-freeze window is active.",
                observed=now.isoformat(),
                limit=[window.model_dump(mode="json") for window in self.config.change_freeze_windows],
            )
        else:
            add_check(
                "change_freeze_window",
                PolicyCheckState.NOT_CONFIGURED,
                "No change-freeze windows are configured.",
            )

    def _evaluate_freeze_labels(
        self,
        source: WorkloadSource,
        add_check,
    ) -> None:
        configured_labels = self.config.change_freeze_labels
        if not configured_labels:
            add_check(
                "change_freeze_labels",
                PolicyCheckState.NOT_CONFIGURED,
                "No change-freeze labels are configured.",
            )
            return

        labels = _workload_labels(source)
        if labels is None:
            add_check(
                "change_freeze_labels",
                PolicyCheckState.REJECTED,
                "Configured change-freeze labels cannot be checked because workload label evidence is unavailable.",
                limit=list(configured_labels),
                code=AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                detail=(
                    "Refresh the workload binding with current workload labels before planning or approval; "
                    "configured change-freeze labels cannot be safely checked."
                ),
            )
            return

        matches = [selector for selector in configured_labels if _label_matches(selector, labels)]
        if matches:
            add_check(
                "change_freeze_labels",
                PolicyCheckState.REJECTED,
                "The workload has a configured change-freeze label.",
                observed=matches,
                limit=list(configured_labels),
                code=AutomationErrorCode.CHANGE_FREEZE_ACTIVE,
                detail=(
                    f"The workload matches change-freeze label(s) {', '.join(matches)}; remove the label(s) "
                    "or wait until the freeze is lifted before retrying."
                ),
            )
        else:
            add_check(
                "change_freeze_labels",
                PolicyCheckState.PASSED,
                "The workload does not match any configured change-freeze label.",
                observed={"label_keys": sorted(labels)},
                limit=list(configured_labels),
            )

    async def _controller_health_exemption(
        self, source: WorkloadSource, records: Sequence[RecommendationRecord]
    ) -> str | None:
        """Why an unhealthy controller must not block this plan, or ``None`` when it must.

        An OOM-killed or CPU-starved workload makes its Argo CD Application Degraded or
        Progressing; blocking its own request/limit increase would keep it broken forever.
        The exemption applies only to an increase-only reliability plan, on a controller that
        is not suspended, with no sync or comparison error, whose live status shows that
        every unhealthy resource is the plan's target workload itself.
        """
        evidence = source.evidence
        if _optional_bool(evidence, "controller_healthy") is not False:
            return None
        if _optional_bool(evidence, "controller_suspended") is True:
            return None
        if source.controller_kind is not ControllerKind.ARGOCD or self.controller_status_reader is None:
            return None
        if not _increase_only_reliability(records):
            return None
        try:
            status = await self.controller_status_reader(source)
        except Exception:
            return None
        if not isinstance(status, Mapping):
            return None
        return _argo_self_caused_health(status, source)

    def _evaluate_controller_health(
        self,
        evidence: Mapping[str, Any],
        add_check,
        add_missing_evidence,
        *,
        exemption: str | None = None,
    ) -> None:
        suspended = _optional_bool(evidence, "controller_suspended")
        healthy = _optional_bool(evidence, "controller_healthy")
        if exemption is not None and suspended is not True and healthy is False:
            add_check(
                "controller_health",
                PolicyCheckState.PASSED,
                exemption,
                observed={"suspended": suspended, "healthy": healthy, "self_caused": True},
                limit={"suspended": False, "healthy": True},
            )
        elif suspended is True or healthy is False:
            add_check(
                "controller_health",
                PolicyCheckState.REJECTED,
                "The owning GitOps controller is suspended or unhealthy.",
                observed={"suspended": suspended, "healthy": healthy},
                limit={"suspended": False, "healthy": True},
                code=AutomationErrorCode.CONTROLLER_UNHEALTHY,
                detail="Restore the owning GitOps controller to a healthy, syncing state before retrying.",
            )
        elif suspended is False and healthy is True:
            add_check(
                "controller_health",
                PolicyCheckState.PASSED,
                "Available controller evidence reports a healthy, syncing controller.",
                observed={"suspended": suspended, "healthy": healthy},
                limit={"suspended": False, "healthy": True},
            )
        else:
            add_missing_evidence(
                "controller_health",
                "Controller health and suspend state are not present in the current workload binding evidence.",
                "Refresh GitOps discovery so the owning controller's health can be checked before planning.",
            )

    def _evaluate_optional_live_gates(
        self,
        source: WorkloadSource,
        records: Sequence[RecommendationRecord],
        add_check,
        add_missing_evidence,
        add_warning,
        *,
        history_start: datetime | None = None,
        step_seconds: int = 300,
        controller_health_exemption: str | None = None,
    ) -> None:
        evidence = source.evidence
        if _resource_bound(records):
            # A kind bound by GVK and name (a resource to delete, H6) has no rollout, no
            # autoscaler or disruption budget and no usage samples: its evidence is the
            # deletion evidence its analyzer required. Controller health still applies.
            self._evaluate_controller_health(evidence, add_check, add_missing_evidence)
            add_check(
                "sample_count_coverage",
                PolicyCheckState.PASSED,
                "Not applicable: the target is not a workload; the deletion evidence was required by its analyzer.",
            )
            return
        rollout = _optional_bool(evidence, "rollout_in_progress")
        if rollout is True:
            add_check(
                "rollout_state",
                PolicyCheckState.REJECTED,
                "The workload has an in-progress rollout.",
                observed=True,
                limit=False,
                code=AutomationErrorCode.ROLLOUT_IN_PROGRESS,
                detail="Wait for the workload rollout to complete before creating or approving a plan.",
            )
        elif rollout is False:
            add_check(
                "rollout_state",
                PolicyCheckState.PASSED,
                "No workload rollout is in progress.",
                observed=False,
                limit=False,
            )
        else:
            add_missing_evidence(
                "rollout_state",
                "Live rollout status is not present in the current workload binding evidence.",
                "Refresh GitOps discovery so the workload rollout state can be checked before planning or approval.",
            )

        self._evaluate_controller_health(
            evidence, add_check, add_missing_evidence, exemption=controller_health_exemption
        )

        if "hpa_targets" in evidence or "pdb_conflict" in evidence or "vpa_targets" in evidence:
            changes, adds_hpa = _planned_resource_changes(records)
            self._evaluate_hpa_gate(evidence, changes, adds_hpa, add_check, add_missing_evidence, add_warning)
            self._evaluate_vpa_gate(evidence, add_check, add_missing_evidence, add_warning)
            self._evaluate_pdb_gate(evidence, add_check, add_missing_evidence)
        else:
            self._evaluate_legacy_interaction_gate(evidence, add_check, add_missing_evidence)

        sample_evidence: list[dict[str, Any]] = []
        invalid_sample_ids: list[int | None] = []
        for record in records:
            evidence = record.evidence
            if evidence is None:
                invalid_sample_ids.append(record.id)
                continue
            sample_count = evidence.sample_count
            coverage = evidence.coverage_ratio
            if (
                type(sample_count) is not int
                or sample_count < 0
                or isinstance(coverage, bool)
                or not isinstance(coverage, (int, float))
                or not math.isfinite(coverage)
                or not 0 <= coverage <= 1
            ):
                invalid_sample_ids.append(record.id)
                continue
            sample_evidence.append(
                {
                    "recommendation_id": record.id,
                    "sample_count": sample_count,
                    "coverage_ratio": coverage,
                    # minCoverage is evaluated against the data available since install (F7).
                    "coverage_since_install": round(coverage_since_history(evidence, history_start, step_seconds), 4),
                }
            )

        if not records or invalid_sample_ids:
            add_check(
                "sample_count_coverage",
                PolicyCheckState.REJECTED,
                "Recommendation sample and coverage evidence is missing or invalid.",
                observed={"missing_or_invalid_recommendation_ids": invalid_sample_ids},
                limit={
                    "min_samples": self.config.min_samples,
                    "min_coverage": self.config.min_coverage,
                },
                code=AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                detail="Refresh the affected recommendations to collect valid sample and coverage evidence.",
            )
        else:
            below_threshold = [
                item
                for item in sample_evidence
                if item["sample_count"] < self.config.min_samples
                or item["coverage_since_install"] < self.config.min_coverage
            ]
            add_check(
                "sample_count_coverage",
                PolicyCheckState.REJECTED if below_threshold else PolicyCheckState.PASSED,
                (
                    "Recommendation sample count or coverage is below the configured minimum."
                    if below_threshold
                    else "Recommendation sample count and coverage meet the configured minimums."
                ),
                observed=sample_evidence,
                limit={
                    "min_samples": self.config.min_samples,
                    "min_coverage": self.config.min_coverage,
                },
                code=AutomationErrorCode.POLICY_EVIDENCE_MISSING if below_threshold else None,
                detail=(
                    "Collect more samples or improve metric coverage before creating or approving this plan."
                    if below_threshold
                    else None
                ),
            )

    def _evaluate_hpa_gate(
        self,
        evidence: Mapping[str, Any],
        changes: set[tuple[str, str]],
        adds_hpa: bool,
        add_check,
        add_missing_evidence,
        add_warning,
    ) -> None:
        """Block only when an HPA scales on a resource the plan changes (D13, BUG-55)."""
        hpas = evidence.get("hpa_targets")
        if evidence.get("hpa_pdb_interactions_checked") is not True or not isinstance(hpas, list):
            add_missing_evidence(
                "hpa_interaction",
                "HorizontalPodAutoscaler evidence is not available for this workload.",
                "Refresh GitOps discovery so HorizontalPodAutoscalers targeting the workload can be checked.",
            )
            return
        valid_hpas = [hpa for hpa in hpas if isinstance(hpa, Mapping) and isinstance(hpa.get("name"), str)]
        if not valid_hpas:
            add_check(
                "hpa_interaction",
                PolicyCheckState.PASSED,
                "No HorizontalPodAutoscaler targets the workload.",
                observed=[],
            )
            return
        conflicts: list[dict[str, Any]] = []
        unaffected: list[str] = []
        for hpa in valid_hpas:
            name = str(hpa["name"])
            if adds_hpa:
                conflicts.append({"hpa": name, "reason": "the plan adds a second autoscaler"})
                continue
            # Only a Utilization target is a share of the request: changing the request moves it.
            # AverageValue/Value targets compare raw usage; an unknown type fails closed.
            scaled = [
                metric
                for metric in hpa.get("resource_metrics") or ()
                if isinstance(metric, Mapping) and metric.get("target_type") not in _REQUEST_INDEPENDENT_HPA_TARGETS
            ]
            matched = sorted(
                {
                    str(metric.get("resource"))
                    for metric in scaled
                    for container, resource in changes
                    if str(metric.get("resource", "")).lower() == resource
                    and (metric.get("container") in (None, "") or metric.get("container") == container)
                }
            )
            if matched:
                conflicts.append({"hpa": name, "resources": matched})
            else:
                unaffected.append(name)
        observed = {
            "hpas": [hpa["name"] for hpa in valid_hpas],
            "planned_resources": sorted(resource for _, resource in changes),
        }
        if conflicts:
            add_check(
                "hpa_interaction",
                PolicyCheckState.REJECTED,
                "A HorizontalPodAutoscaler scales on a resource this plan changes.",
                observed={**observed, "conflicts": conflicts},
                code=AutomationErrorCode.POLICY_INTERACTION_CONFLICT,
                detail=(
                    "An HPA scales this workload on a resource the plan changes; changing its requests would shift "
                    "the autoscaling target. Tune the HPA or choose a recommendation for another resource."
                ),
            )
            return
        add_check(
            "hpa_interaction",
            PolicyCheckState.PASSED,
            "HorizontalPodAutoscalers target the workload but do not scale on the resources this plan changes.",
            observed=observed,
        )
        add_warning(
            AutomationErrorCode.POLICY_INTERACTION_CONFLICT,
            f"HPA {', '.join(sorted(unaffected))} also manages this workload; it does not scale on a share of the "
            "changed requests, but review the replica range after the change.",
        )

    def _evaluate_vpa_gate(self, evidence: Mapping[str, Any], add_check, add_missing_evidence, add_warning) -> None:
        """A VPA that writes requests itself is a conflicting writer for a Git change (D13)."""
        vpas = evidence.get("vpa_targets")
        if evidence.get("vpa_interactions_checked") is not True or not isinstance(vpas, list):
            add_missing_evidence(
                "vpa_interaction",
                "VerticalPodAutoscaler evidence is not available for this workload.",
                "Refresh GitOps discovery so VerticalPodAutoscalers targeting the workload can be checked.",
            )
            return
        valid = [vpa for vpa in vpas if isinstance(vpa, Mapping) and isinstance(vpa.get("name"), str)]
        writers = [
            {"vpa": vpa["name"], "update_mode": vpa.get("update_mode")}
            for vpa in valid
            if vpa.get("update_mode") in _CONFLICTING_VPA_UPDATE_MODES
        ]
        if writers:
            add_check(
                "vpa_interaction",
                PolicyCheckState.REJECTED,
                "A VerticalPodAutoscaler actively writes this workload's resource requests.",
                observed=writers,
                limit=sorted(_CONFLICTING_VPA_UPDATE_MODES),
                code=AutomationErrorCode.POLICY_INTERACTION_CONFLICT,
                detail=(
                    "A VPA in an active update mode rewrites pod requests and would override this change; set its "
                    "updateMode to Off or remove it before creating a GitOps plan."
                ),
            )
            return
        add_check(
            "vpa_interaction",
            PolicyCheckState.PASSED,
            "No VerticalPodAutoscaler actively writes this workload's requests.",
            observed=[{"vpa": vpa["name"], "update_mode": vpa.get("update_mode")} for vpa in valid],
        )
        initial = sorted(str(vpa["name"]) for vpa in valid if vpa.get("update_mode") == "Initial")
        if initial:
            add_warning(
                AutomationErrorCode.POLICY_INTERACTION_CONFLICT,
                f"VPA {', '.join(initial)} sets requests when pods are created (updateMode Initial).",
            )

    def _evaluate_pdb_gate(self, evidence: Mapping[str, Any], add_check, add_missing_evidence) -> None:
        conflict = _optional_bool(evidence, "pdb_conflict")
        if evidence.get("hpa_pdb_interactions_checked") is not True or conflict is None:
            add_missing_evidence(
                "pdb_interaction",
                "PodDisruptionBudget evidence is not available for this workload.",
                "Refresh GitOps discovery so PodDisruptionBudgets selecting the workload can be checked.",
            )
        elif conflict:
            add_check(
                "pdb_interaction",
                PolicyCheckState.REJECTED,
                "A PodDisruptionBudget blocks the rolling restart this change requires.",
                observed=True,
                limit=False,
                code=AutomationErrorCode.POLICY_INTERACTION_CONFLICT,
                detail="Relax the PodDisruptionBudget (or add replicas) so the workload can roll before retrying.",
            )
        else:
            add_check(
                "pdb_interaction",
                PolicyCheckState.PASSED,
                "No PodDisruptionBudget blocks a rolling restart of the workload.",
                observed=False,
                limit=False,
            )

    def _evaluate_legacy_interaction_gate(self, evidence: Mapping[str, Any], add_check, add_missing_evidence) -> None:
        """Bindings discovered before 0.4 carry one aggregated HPA/PDB verdict."""
        interactions_checked = _optional_bool(evidence, "hpa_pdb_interactions_checked")
        interaction_conflict = _optional_bool(evidence, "hpa_pdb_conflict")
        if interaction_conflict is True:
            add_check(
                "hpa_vpa_pdb_interactions",
                PolicyCheckState.REJECTED,
                "Available workload evidence reports an HPA, VPA or PDB interaction conflict.",
                observed=True,
                limit=False,
                code=AutomationErrorCode.POLICY_INTERACTION_CONFLICT,
                detail=("Resolve the reported HPA/VPA/PDB interaction before creating or approving this plan."),
            )
        elif interactions_checked is True or interaction_conflict is False:
            add_check(
                "hpa_vpa_pdb_interactions",
                PolicyCheckState.PASSED,
                "Available workload evidence reports no HPA/VPA/PDB interaction conflict.",
                observed=False,
                limit=False,
            )
        else:
            add_missing_evidence(
                "hpa_vpa_pdb_interactions",
                "HPA, VPA and PDB interaction evidence is not available to the plan service.",
                "Refresh GitOps discovery so HPA, VPA and PDB interactions can be checked before planning.",
            )

    def _reject_missing_history(self, add_check) -> None:
        detail = (
            "Workload plan history is unavailable, so open-PR, active-plan and cooldown limits cannot be checked; "
            "restore plan-history storage before retrying."
        )
        for gate in ("open_pull_request_budget", "active_plan", "cooldown"):
            add_check(
                gate,
                PolicyCheckState.REJECTED,
                "Required workload plan-history evidence is unavailable.",
                code=AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                detail=detail,
            )

    async def _evaluate_cooldown(
        self,
        plans: Sequence[Plan],
        now: datetime,
        add_check,
        *,
        updated_at_by_plan: Mapping[str, datetime | None] | None = None,
    ) -> None:
        cooldown = timedelta(minutes=self.config.cooldown_minutes)
        if cooldown <= timedelta(0):
            add_check(
                "cooldown",
                PolicyCheckState.PASSED,
                "The configured workload cooldown is disabled.",
                observed=0,
                limit=0,
            )
            return

        candidates = [plan for plan in plans if plan.status in _COOLDOWN_STATUSES]
        if not candidates:
            add_check(
                "cooldown",
                PolicyCheckState.PASSED,
                "No recent pull-request activity was found for this workload.",
                observed=None,
                limit=self.config.cooldown_minutes,
            )
            return

        timestamps: list[datetime] = []
        for plan in candidates:
            timestamp = plan.created_at
            if updated_at_by_plan is not None and plan.plan_id in updated_at_by_plan:
                timestamp = updated_at_by_plan[plan.plan_id] or timestamp
            elif self.plan_repository is not None:
                try:
                    record = await self.plan_repository.get_plan_record(plan.plan_id)
                except Exception:
                    record = None
                if record is None:
                    add_check(
                        "cooldown",
                        PolicyCheckState.REJECTED,
                        "Recent workload plan history could not be read.",
                        observed=plan.plan_id,
                        limit=self.config.cooldown_minutes,
                        code=AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                        detail=(
                            "Refresh plan-history storage so the workload cooldown can be checked before retrying."
                        ),
                    )
                    return
                timestamp = record.updated_at or timestamp
            timestamps.append(ensure_utc(timestamp))

        most_recent = max(timestamps)
        eligible_at = most_recent + cooldown
        remaining = max(timedelta(0), eligible_at - now)
        if now < eligible_at:
            minutes_remaining = max(1, math.ceil(remaining.total_seconds() / 60))
            add_check(
                "cooldown",
                PolicyCheckState.REJECTED,
                "The workload is inside its configured change cooldown.",
                observed={
                    "last_pull_request_activity": most_recent.isoformat(),
                    "retry_at": eligible_at.isoformat(),
                    "minutes_remaining": minutes_remaining,
                },
                limit=self.config.cooldown_minutes,
                code=AutomationErrorCode.WORKLOAD_COOLDOWN_ACTIVE,
                detail=(
                    f"The workload cooldown is active for another {minutes_remaining} minute(s); "
                    f"retry at {eligible_at.isoformat()}."
                ),
            )
        else:
            add_check(
                "cooldown",
                PolicyCheckState.PASSED,
                "The workload cooldown has elapsed.",
                observed=most_recent.isoformat(),
                limit=self.config.cooldown_minutes,
            )


def _record_risk(record: RecommendationRecord) -> RiskLevel | None:
    value: Any = record.risk_level
    if value is None:
        return None
    if isinstance(value, RiskLevel):
        return value
    if isinstance(value, str):
        try:
            return RiskLevel(value.lower())
        except ValueError:
            return None
    return None


def _record_confidence(record: RecommendationRecord) -> float | None:
    value: Any = record.confidence
    if value is None and record.evidence is not None:
        value = record.evidence.confidence
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    return confidence


def _workload_labels(source: WorkloadSource) -> dict[str, str] | None:
    evidence = source.evidence
    value = evidence.get("workload_labels")
    if isinstance(value, Mapping) and all(
        isinstance(name, str) and isinstance(label, str) for name, label in value.items()
    ):
        return dict(value)
    workload = evidence.get("workload")
    if isinstance(workload, Mapping):
        value = workload.get("labels")
        if isinstance(value, Mapping) and all(
            isinstance(name, str) and isinstance(label, str) for name, label in value.items()
        ):
            return dict(value)
    return None


def _label_matches(selector: str, labels: Mapping[str, str]) -> bool:
    if "=" in selector:
        key, expected = selector.split("=", 1)
        return labels.get(key) == expected
    return selector in labels


def _planned_resource_changes(records: Sequence[RecommendationRecord]) -> tuple[set[tuple[str, str]], bool]:
    """(container, resource) pairs whose requests or limits the planned action sets."""
    changes: set[tuple[str, str]] = set()
    adds_hpa = False
    for record in records:
        action = record.action
        if action is None:
            continue
        for operation in action.operations:
            spec = operation_kind_of(operation)
            if spec is not None:
                changes.update(spec.changed_fields([operation]))
            elif isinstance(operation, AddHorizontalPodAutoscaler):
                adds_hpa = True
    return changes, adds_hpa


#: Reads the live status of a workload's owning controller (Argo CD ``Application.status``).
ControllerStatusReader = Callable[[WorkloadSource], Awaitable[Mapping[str, Any] | None]]

_SELF_CAUSED_HEALTH = frozenset({"Degraded", "Progressing"})
_FAILED_OPERATION_PHASES = frozenset({"Failed", "Error"})
_RELIABILITY_TYPES = frozenset({RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY})


def _increase_only_reliability(records: Sequence[RecommendationRecord]) -> bool:
    """Every record raises a container request and never lowers or tightens a limit."""
    if not records:
        return False
    for record in records:
        if record.type not in _RELIABILITY_TYPES:
            return False
        if record.type is RecommendationType.RIGHTSIZING_CPU:
            current, proposed = record.current_cpu_request_millicores, record.recommended_cpu_request_millicores
            resource = "cpu"
        else:
            current, proposed = record.current_memory_request_bytes, record.recommended_memory_request_bytes
            resource = "memory"
        if not isinstance(current, int) or not isinstance(proposed, int) or proposed <= current:
            return False
        for change in getattr(record.evidence, "limit_changes", None) or []:
            if change.action == "remove":
                # Removing a CPU limit only relaxes throttling; a memory limit is never removed.
                if change.resource != "cpu":
                    return False
                continue
            if change.proposed is None or (change.current is not None and change.proposed < change.current):
                return False
        if record.action is not None:
            for operation in record.action.operations:
                kind = operation_kind_of(operation)
                if kind is None or getattr(kind, "kind", None) != "container_resources":
                    return False
            if resource not in {field for _, field in _record_changed_fields(record)}:
                return False
    return True


def _record_changed_fields(record: RecommendationRecord) -> set[tuple[str, str]]:
    action = record.action
    if action is None:
        return set()
    changed: set[tuple[str, str]] = set()
    for operation in action.operations:
        spec = operation_kind_of(operation)
        if spec is not None:
            changed.update(spec.changed_fields([operation]))
    return changed


def _argo_self_caused_health(status: Mapping[str, Any], source: WorkloadSource) -> str | None:
    """An explanation when the Application is unhealthy only because of the target workload."""
    health = status.get("health")
    app_health = health.get("status") if isinstance(health, Mapping) else None
    if app_health not in _SELF_CAUSED_HEALTH:
        return None
    for condition in status.get("conditions") or []:
        kind = condition.get("type") if isinstance(condition, Mapping) else None
        if not isinstance(kind, str) or kind.endswith("Error"):
            return None
    operation = status.get("operationState")
    if isinstance(operation, Mapping) and operation.get("phase") in _FAILED_OPERATION_PHASES:
        return None
    resources = status.get("resources")
    if not isinstance(resources, list):
        return None
    target_health: str | None = None
    target_message = ""
    for resource in resources:
        if not isinstance(resource, Mapping):
            return None
        resource_health = resource.get("health")
        state = resource_health.get("status") if isinstance(resource_health, Mapping) else None
        if state in (None, "Healthy"):
            continue
        is_target = (
            resource.get("kind") == source.workload_kind
            and resource.get("name") == source.workload_name
            and (resource.get("namespace") or source.namespace) == source.namespace
            and resource.get("group") in (None, "", "apps")
        )
        if not is_target or state not in _SELF_CAUSED_HEALTH:
            return None
        target_health = state
        message = resource_health.get("message") if isinstance(resource_health, Mapping) else None
        target_message = f" ({str(message)[:200]})" if message else ""
    if target_health is None:
        return None
    return (
        f"The owning Argo CD Application is {app_health} only because the target "
        f"{source.workload_kind} {source.namespace}/{source.workload_name} is {target_health}{target_message}; "
        "no sync error and no other resource is unhealthy. This increase-only reliability fix is the remedy, "
        "so the controller health gate does not block it."
    )


def _optional_bool(evidence: Mapping[str, Any], key: str) -> bool | None:
    value = evidence.get(key)
    return value if isinstance(value, bool) else None


def _same_workload(left: WorkloadSource, right: WorkloadSource) -> bool:
    return (
        left.cluster,
        left.namespace,
        left.workload_kind,
        left.workload_name,
    ) == (
        right.cluster,
        right.namespace,
        right.workload_kind,
        right.workload_name,
    )


def _resource_bound(records: Sequence[RecommendationRecord]) -> bool:
    """Whether the records' action kind binds its target by GVK and name (not a workload, H0d)."""
    from greenkube.automation.actions import action_kind_of

    specs = [action_kind_of(record.action) if record.action is not None else None for record in records]
    return bool(specs) and all(spec is not None and spec.binding_scope == "resource" for spec in specs)
