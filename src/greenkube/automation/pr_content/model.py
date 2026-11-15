"""Inputs and settings of pull-request content rendering (roadmap J1, J2)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from greenkube.automation.actions import operation_kind_of
from greenkube.automation.pr_content.template import DEFAULT_TEMPLATE, load_template
from greenkube.gitops.contracts import (
    AddHorizontalPodAutoscaler,
    DeleteResource,
    Plan,
)
from greenkube.models.metrics import RecommendationRecord
from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

DEFAULT_TITLE_PREFIX = "greenkube"
MAX_TITLE_CHARS = 72
MAX_BODY_CHARS = 60_000
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True, slots=True)
class PullRequestRenderSettings:
    """Operator settings that shape titles, bodies and lifecycle comments."""

    title_prefix: str = DEFAULT_TITLE_PREFIX
    external_url: str | None = None
    template: str = DEFAULT_TEMPLATE
    max_title_chars: int = MAX_TITLE_CHARS
    max_body_chars: int = MAX_BODY_CHARS
    lifecycle_comments: bool = True
    change_measurement_enabled: bool = True
    verification_window_hours: float = 72.0
    measurement_pre_window_days: float = 7.0
    measurement_post_window_days: float = 7.0
    measurement_warmup_hours: float = 1.0

    @classmethod
    def from_config(cls, config: Any | None = None) -> PullRequestRenderSettings:
        """Read the settings from :class:`greenkube.core.config.Config`."""
        if config is None:
            from greenkube.core.config import get_config

            config = get_config()
        return cls(
            title_prefix=str(getattr(config, "GITOPS_PR_TITLE_PREFIX", DEFAULT_TITLE_PREFIX) or "").strip(),
            external_url=external_url(getattr(config, "GREENKUBE_EXTERNAL_URL", None)),
            template=load_template(getattr(config, "GITOPS_PR_TEMPLATE", None)),
            lifecycle_comments=bool(getattr(config, "GITOPS_PR_LIFECYCLE_COMMENTS", True)),
            change_measurement_enabled=bool(getattr(config, "CHANGE_MEASUREMENT_ENABLED", True)),
            verification_window_hours=float(getattr(config, "VERIFICATION_WINDOW_HOURS", 72.0)),
            measurement_pre_window_days=float(getattr(config, "CHANGE_MEASUREMENT_PRE_WINDOW_DAYS", 7.0)),
            measurement_post_window_days=float(getattr(config, "CHANGE_MEASUREMENT_POST_WINDOW_DAYS", 7.0)),
            measurement_warmup_hours=float(getattr(config, "CHANGE_MEASUREMENT_WARMUP_HOURS", 1.0)),
        )

    def link(self, path: str) -> str | None:
        """Absolute deep link into the GreenKube UI, or ``None`` without an external URL."""
        if not self.external_url:
            return None
        return f"{self.external_url}/{path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class PullRequestFeatures:
    """Which derived PR metadata the plan worker publishes (J2, J3, J5)."""

    default_labels: bool = True
    draft_high_risk: bool = True
    codeowners: bool = True
    commit_status: bool = True

    @classmethod
    def from_config(cls, config: Any | None = None) -> PullRequestFeatures:
        if config is None:
            from greenkube.core.config import get_config

            config = get_config()
        return cls(
            default_labels=bool(getattr(config, "GITOPS_PR_DEFAULT_LABELS", True)),
            draft_high_risk=bool(getattr(config, "GITOPS_PR_DRAFT_HIGH_RISK", True)),
            codeowners=bool(getattr(config, "GITOPS_PR_CODEOWNERS", True)),
            commit_status=bool(getattr(config, "GITOPS_PR_COMMIT_STATUS", True)),
        )


def external_url(value: object) -> str | None:
    """Normalize ``GREENKUBE_EXTERNAL_URL``: an http(s) origin and optional path, no trailing slash."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().rstrip("/")
    if not text.startswith(("https://", "http://")) or any(character.isspace() for character in text):
        return None
    return text


@dataclass(frozen=True, slots=True)
class PullRequestContext:
    """Everything one plan pull request is rendered from."""

    plan: Plan
    repository: str
    branch: str
    recommendations: Sequence[RecommendationRecord] = ()
    missing_recommendation_ids: Sequence[int] = ()
    planned_base_revision: str | None = None

    @property
    def base_revision(self) -> str:
        file_plan = self.plan.file_plan
        if self.planned_base_revision:
            return self.planned_base_revision
        return file_plan.base_revision if file_plan is not None else ""

    @property
    def is_rollback(self) -> bool:
        return self.plan.rollback_of_plan_id is not None

    @property
    def workload(self) -> str:
        target = self.plan.action.target
        return f"{target.namespace}/{target.name}" if target.namespace else target.name


@dataclass(frozen=True, slots=True)
class ResourceChange:
    """One container resource that the plan changes (current → proposed)."""

    container: str
    field: str  # e.g. requests.cpu, limits.memory
    proposed_text: str
    current: int | None = None
    proposed: int | None = None

    @property
    def resource(self) -> str:
        return self.field.split(".", 1)[-1]

    @property
    def is_cpu(self) -> bool:
        return self.resource == "cpu"

    @property
    def increases(self) -> bool:
        return self.current is not None and self.proposed is not None and self.proposed > self.current

    @property
    def unchanged(self) -> bool:
        return self.current is not None and self.current == self.proposed


@dataclass(frozen=True, slots=True)
class SavingsTotals:
    """Projected annual savings of a plan, never mixed with measured values."""

    currency: str
    cost_annual: float = 0.0
    co2e_annual_g: float = 0.0
    has_cost: bool = False
    has_co2e: bool = False
    realization_factors: tuple[float, ...] = ()
    allocated_cost_annual: float | None = None
    currencies: tuple[str, ...] = field(default_factory=tuple)
    #: Recommendations whose projection spans several currencies (``currency: mixed``):
    #: their cost cannot be expressed in one currency and is left out of the totals.
    mixed_currency_records: int = 0


def resource_changes(context: PullRequestContext) -> list[ResourceChange]:
    """Changed container resources, current values from the pre-apply baseline or evidence."""
    plan = context.plan
    changes: list[ResourceChange] = []
    for operation in plan.action.operations:
        # Each action kind lists the fields it sets (H0): no per-operation-type branching here.
        spec = operation_kind_of(operation)
        if spec is None:
            continue
        for field_change in spec.pr_section([operation]).changes:
            kind, _, resource = field_change.field.partition(".")
            change = ResourceChange(
                container=field_change.subject,
                field=field_change.field,
                proposed_text=field_change.proposed,
                current=_current_value(context, field_change.subject, kind, resource),
                proposed=_parse_quantity(resource, field_change.proposed),
            )
            if not change.unchanged:
                changes.append(change)
    return changes


def _parse_quantity(resource: str, quantity: str) -> int | None:
    if resource == "cpu":
        return parse_cpu_quantity(quantity)
    if resource == "memory":
        return parse_memory_quantity(quantity)
    return None


def _current_value(context: PullRequestContext, container: str, kind: str, resource: str) -> int | None:
    baseline = context.plan.pre_apply_baseline
    if kind == "requests" and baseline is not None and container in baseline.container_requests:
        requests = baseline.container_requests[container]
        value = (
            requests.cpu_millicores if resource == "cpu" else requests.memory_bytes if resource == "memory" else None
        )
        if value is not None:
            return value
    for record in context.recommendations:
        if record.container_name not in (None, container) or record.evidence is None:
            continue
        current = record.evidence.current
        value = {
            ("requests", "cpu"): current.cpu_request_millicores,
            ("requests", "memory"): current.memory_request_bytes,
            ("limits", "cpu"): current.cpu_limit_millicores,
            ("limits", "memory"): current.memory_limit_bytes,
        }.get((kind, resource))
        if value is not None:
            return value
    return None


def savings_totals(recommendations: Sequence[RecommendationRecord]) -> SavingsTotals:
    """Sum projected annual savings; realization-adjusted Impact Model values first."""
    by_currency: dict[str, list[float]] = {}
    co2e_total = 0.0
    has_cost = has_co2e = False
    factors: list[float] = []
    allocated = 0.0
    has_allocated = False
    mixed = 0
    for record in recommendations:
        impact = record.impact if isinstance(record.impact, dict) else {}
        currency = str(impact.get("currency") or "USD").upper()
        if currency == "MIXED":
            # The Impact Model summed prices of several currencies: no single-currency figure exists.
            mixed += 1
        else:
            cost = _finite(impact.get("expected_billable_cost_annual"))
            if cost is None:
                cost = _finite(record.potential_savings_cost)
            if cost is not None:
                by_currency.setdefault(currency, []).append(cost)
                has_cost = True
        allocated_cost = _finite(impact.get("allocated_cost_annual")) if currency != "MIXED" else None
        if allocated_cost is not None:
            allocated += allocated_cost
            has_allocated = True
        co2e = _finite(impact.get("co2e_expected_annual_g"))
        if co2e is None:
            co2e = _finite(record.potential_savings_co2e_grams)
        if co2e is not None:
            co2e_total += co2e
            has_co2e = True
        factor = _finite(impact.get("realization_factor"))
        if factor is not None and factor not in factors:
            factors.append(factor)
    currencies = tuple(sorted(by_currency))
    currency = currencies[0] if currencies else "USD"
    return SavingsTotals(
        currency=currency,
        cost_annual=sum(by_currency.get(currency, [])),
        co2e_annual_g=co2e_total,
        has_cost=has_cost,
        has_co2e=has_co2e,
        realization_factors=tuple(factors),
        allocated_cost_annual=allocated if has_allocated and len(currencies) <= 1 else None,
        currencies=currencies,
        mixed_currency_records=mixed,
    )


@dataclass(frozen=True, slots=True)
class CapacityTotals:
    """Projected released capacity of a plan (WP-08.1): reserved capacity, never money.

    Negative values are increases. ``None`` means no recommendation reports the figure.
    """

    cpu_cores: float | None = None
    mem_gib: float | None = None
    cpu_core_hours: float | None = None
    mem_gib_hours: float | None = None
    storage_gib: float | None = None
    load_balancers: float | None = None

    @property
    def empty(self) -> bool:
        return all(
            value is None or abs(value) < 1e-9
            for value in (
                self.cpu_cores,
                self.mem_gib,
                self.cpu_core_hours,
                self.mem_gib_hours,
                self.storage_gib,
                self.load_balancers,
            )
        )


def capacity_totals(recommendations: Sequence[RecommendationRecord]) -> CapacityTotals:
    """Sum the projected released capacity of the plan's recommendations, unit by unit."""
    sums: dict[str, float] = {}
    for record in recommendations:
        capacity = record.released_capacity
        if not capacity:
            continue
        annual = capacity.get("annual") or {}
        instantaneous = capacity.get("instantaneous") or {}
        for key, value in (
            ("cpu_cores", instantaneous.get("cpu_cores")),
            ("mem_gib", instantaneous.get("mem_gib")),
            ("cpu_core_hours", annual.get("cpu_core_hours")),
            ("mem_gib_hours", annual.get("mem_gib_hours")),
            ("storage_gib", instantaneous.get("storage_gib")),
            ("load_balancers", instantaneous.get("load_balancers")),
        ):
            number = _finite(value)
            if number is not None and abs(number) > 1e-9:
                sums[key] = sums.get(key, 0.0) + number
    return CapacityTotals(**sums)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def raises_requests(context: PullRequestContext) -> bool:
    """Whether the plan only raises requests: a reliability increase (H1), shown as a cost, never a saving."""
    if context.is_rollback:
        return False
    changes = [change for change in resource_changes(context) if change.field.startswith("requests.")]
    return any(change.increases for change in changes) and not any(
        change.proposed is not None and change.current is not None and change.proposed < change.current
        for change in changes
    )


def edits_limits(context: PullRequestContext) -> bool:
    """Whether the plan sets or removes a container limit (H1); such edits are reverted by reverting the PR."""
    return any(change.field.startswith("limits.") for change in resource_changes(context))


def plan_verb(context: PullRequestContext) -> str:
    """The action verb used in titles and summaries."""
    operations = context.plan.action.operations
    if any(isinstance(operation, DeleteResource) for operation in operations):
        verb = "delete"
    elif any(isinstance(operation, AddHorizontalPodAutoscaler) for operation in operations):
        verb = "autoscale"
    elif any(change.increases for change in resource_changes(context)) and not any(
        change.proposed is not None and change.current is not None and change.proposed < change.current
        for change in resource_changes(context)
    ):
        verb = "raise resources of"
    else:
        verb = "rightsize"
    return f"revert {verb}" if context.is_rollback else verb


def highest_risk(recommendations: Sequence[RecommendationRecord]) -> str | None:
    levels = [_risk_value(record.risk_level) for record in recommendations]
    known = [level for level in levels if level in _RISK_ORDER]
    return max(known, key=lambda level: _RISK_ORDER[level]) if known else None


def _risk_value(level: object) -> str | None:
    value = getattr(level, "value", level)
    return value.lower() if isinstance(value, str) else None
