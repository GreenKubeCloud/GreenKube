"""Fail-closed policy evaluation for GitOps automation plans."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol, cast

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from greenkube.gitops.contracts import AutomationErrorCode, Plan, PlanStatus, WorkloadSource
from greenkube.models.metrics import RecommendationRecord, RiskLevel
from greenkube.storage.base_gitops_repository import AutomationPlanRecord

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
    ) -> None:
        self.config = config
        self.plan_repository = plan_repository
        self.clock = clock

    async def evaluate(
        self,
        records: Sequence[RecommendationRecord],
        source: WorkloadSource,
        *,
        exclude_plan_id: str | None = None,
        exclude_plan_ids: Sequence[str] = (),
    ) -> AutomationPolicyResult:
        now = _as_utc(self.clock())
        workload = {
            "cluster": source.cluster,
            "namespace": source.namespace,
            "kind": source.workload_kind,
            "name": source.workload_name,
        }
        checks: list[PolicyCheck] = []
        rejections: list[PolicyRejection] = []

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
        self._evaluate_optional_live_gates(source, records, add_check)

        if self.plan_repository is None:
            self._reject_missing_history(add_check)
        else:
            try:
                workload_history_reader = getattr(self.plan_repository, "list_plans_for_workload", None)
                if callable(workload_history_reader):
                    plans = await cast(Any, workload_history_reader)(
                        cluster=source.cluster,
                        namespace=source.namespace,
                        workload_kind=source.workload_kind,
                        workload_name=source.workload_name,
                        statuses=_HISTORY_STATUSES,
                    )
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
                )

        return AutomationPolicyResult(
            allowed=not rejections,
            evaluated_at=now,
            workload=workload,
            policy=self.config.public_values(),
            checks=checks,
            rejections=rejections,
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

    def _evaluate_optional_live_gates(
        self,
        source: WorkloadSource,
        records: Sequence[RecommendationRecord],
        add_check,
    ) -> None:
        evidence = source.evidence
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
            add_check(
                "rollout_state",
                PolicyCheckState.NOT_AVAILABLE,
                "Live rollout status is not present in the current workload binding evidence.",
            )

        suspended = _optional_bool(evidence, "controller_suspended")
        healthy = _optional_bool(evidence, "controller_healthy")
        if suspended is True or healthy is False:
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
            add_check(
                "controller_health",
                PolicyCheckState.NOT_AVAILABLE,
                "Controller health and suspend state are not present in the current workload binding evidence.",
            )

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
            add_check(
                "hpa_vpa_pdb_interactions",
                PolicyCheckState.NOT_AVAILABLE,
                "HPA, VPA and PDB interaction evidence is not available to the plan service.",
            )

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
                if item["sample_count"] < self.config.min_samples or item["coverage_ratio"] < self.config.min_coverage
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

    async def _evaluate_cooldown(self, plans: Sequence[Plan], now: datetime, add_check) -> None:
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
            if self.plan_repository is not None:
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
            timestamps.append(_as_utc(timestamp))

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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
