# src/greenkube/core/optimization/eligibility.py
"""Server-side delivery eligibility of a recommendation (roadmap F10, F7).

Every recommendation says whether GreenKube can open a pull request for it
and, when it cannot, why and what would make it eligible (principle 6, fail
closed, explain why)::

    {"can_open_pr": false,
     "delivery_modes": ["manual"],
     "blockers": [{"code": "warming_up", "message": "...", "fix": "..."}],
     "warnings": [{"code": "...", "message": "...", "fix": "..."}],
     "eligible_at": "2026-11-09T12:00:00+00:00",
     "action_kind": "container_resources",
     "recommendation_ids": [12, 13]}

The checks, in order: lifecycle status and open pull requests, a supported
action kind with a typed container target, automation configuration, the
workload's GitOps binding, the warm-up of usage evidence, and the automation
policy gates (the same ``AutomationPolicyEvaluator`` that guards plan
creation, run read-only). Eligibility depends on live state (bindings, freeze
windows, open pull requests, the clock), so it is computed when a
recommendation is read and never persisted. Codes are documented in
``docs/reference/eligibility-codes.md``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from greenkube.automation.policy import AutomationPolicyConfig, AutomationPolicyEvaluator, AutomationPolicyResult
from greenkube.core.deletion_safety import DELETION_TYPES
from greenkube.core.optimization.risks import profile_from_binding_evidence
from greenkube.gitops.contracts import BindingStatus
from greenkube.storage.base_gitops_repository import WorkloadBinding, freshen_workload_binding

logger = logging.getLogger(__name__)

ACTION_CONTAINER_RESOURCES = "container_resources"

#: Recommendation types GreenKube can turn into a pull request, by action kind.
SUPPORTED_ACTION_KINDS: Dict[str, str] = {
    "RIGHTSIZING_CPU": ACTION_CONTAINER_RESOURCES,
    "RIGHTSIZING_MEMORY": ACTION_CONTAINER_RESOURCES,
}

#: Action kinds planned for the other types (roadmap H), named in the blocker.
PLANNED_ACTION_KINDS: Dict[str, Tuple[str, str]] = {
    "AUTOSCALING_CANDIDATE": ("hpa", "H3"),
    "OFF_PEAK_SCALING": ("schedule", "H4"),
    "ZOMBIE_POD": ("deletion", "H6"),
    "IDLE_NAMESPACE": ("deletion", "H6"),
    "ORPHANED_PERSISTENT_VOLUME": ("deletion", "H6"),
    "ORPHANED_LOAD_BALANCER": ("deletion", "H6"),
    "OVERPROVISIONED_NODE": ("node_pool", "H7"),
    "UNDERUTILIZED_NODE": ("node_pool", "H7"),
    "CARBON_AWARE_SCHEDULING": ("advisory", "E4"),
}

DELIVERY_DIRECT = "direct"
DELIVERY_MANUAL = "manual"

#: Policy gates evaluated locally (for every recommendation, with or without a binding).
_LOCAL_GATES = frozenset({"maximum_risk", "minimum_confidence", "change_freeze_window", "sample_count_coverage"})
#: Policy gates reported as warnings: an unapproved preview of the workload is reused or
#: replaced when a new one is prepared (D6), so it must not hide the preview action.
_WARNING_GATES = frozenset({"active_plan"})

#: Default message and fix of each code (docs/reference/eligibility-codes.md).
CODES: Dict[str, Tuple[str, str]] = {
    "not_active": (
        "Only active recommendations can become a pull request.",
        "Restore the recommendation (un-ignore it) or wait for the next scan to refresh it.",
    ),
    "pull_request_open": (
        "A pull request is already open for this recommendation.",
        "Review and merge or close the open pull request.",
    ),
    "deletion_requires_review": (
        "Deleting a resource is never automated: GreenKube can show why it appears unused, not prove that "
        "nothing depends on it.",
        "Check the evidence in the recommendation, confirm with the owners, then delete the resource from Git "
        "(or with kubectl for resources outside GitOps) and mark the recommendation as applied. Annotate the "
        'resource with greenkube.io/ignore="true" if it is still needed.',
    ),
    "action_kind_unsupported": (
        "GreenKube cannot open pull requests for this kind of change yet.",
        "Apply the change manually, then mark the recommendation as applied so it is measured.",
    ),
    "action_unavailable": (
        "No typed container-resources change could be built for this recommendation.",
        "The next scan rebuilds the action once the workload owner, the container and a request decrease are "
        "known; otherwise apply the change manually.",
    ),
    "requires_container_target": (
        "The proposed change does not target one named container of the workload.",
        "Rescan once the workload's container metrics are collected; GreenKube never patches a whole pod spec.",
    ),
    "automation_not_configured": (
        "GitOps automation is not configured, so no pull request can be prepared.",
        "Enable automation and configure a Git provider (see docs/automation.md).",
    ),
    "automation_disabled": (
        "GitOps automation is disabled.",
        "Set automation.enabled=true with an automation mode other than disabled.",
    ),
    "binding_unavailable": (
        "The workload's GitOps binding could not be read.",
        "Check the database connection and GET /api/v1/gitops/status, then retry.",
    ),
    "pull_requests_unavailable": (
        "The open pull requests could not be read, so a duplicate pull request cannot be ruled out.",
        "Check the database connection, then retry.",
    ),
    "policy_evaluation_unavailable": (
        "The automation policy could not be evaluated for this workload.",
        "Check the database connection (the policy reads the workload's plan history), then retry.",
    ),
    "binding_missing": (
        "No GitOps source is known for this workload.",
        "Make sure Argo CD or Flux manages the workload, then run POST /api/v1/gitops/refresh.",
    ),
    "ambiguous_binding": (
        "Several GitOps sources could manage this workload.",
        "Choose the source with POST /api/v1/gitops/bindings/{namespace}/{kind}/{name}/resolve.",
    ),
    "binding_unresolved": (
        "GitOps discovery could not find the source of this workload.",
        "Check that the workload's Argo CD Application or Flux Kustomization/HelmRelease is synced, then run "
        "POST /api/v1/gitops/refresh, or resolve the binding manually.",
    ),
    "binding_blocked": (
        "The workload's GitOps source is not supported or not allowed.",
        "Read the binding reason in GET /api/v1/gitops/bindings and adjust the provider allowlist or source.",
    ),
    "binding_stale": (
        "The workload's GitOps binding is out of date.",
        "Run POST /api/v1/gitops/refresh; bindings expire after automation.bindingTtl.",
    ),
    "warming_up": (
        "Collecting usage evidence before a change can be proposed safely.",
        "Wait until enough samples are collected; no action is needed.",
    ),
    "policy_evidence_missing": (
        "Evidence required by the automation policy is missing.",
        "Rescan the recommendation and refresh GitOps discovery so the policy can check it.",
    ),
    "policy_invalid": (
        "The automation policy configuration (GITOPS_POLICY_JSON) is invalid.",
        "Fix automation.policy in the Helm values.",
    ),
    "policy_risk_exceeded": (
        "The recommendation's risk exceeds the automation policy maximum.",
        "Review it and apply it manually, or raise automation.policy.maxRiskLevel.",
    ),
    "policy_confidence_too_low": (
        "The recommendation's confidence is below the automation policy minimum.",
        "Wait for more evidence, or lower automation.policy.minConfidence.",
    ),
    "change_freeze_active": (
        "A change freeze applies to this workload.",
        "Wait until the freeze ends or remove the freeze label.",
    ),
    "workload_pr_limit_exceeded": (
        "The workload already has the maximum number of open pull requests.",
        "Merge or close an open pull request for this workload.",
    ),
    "workload_cooldown_active": (
        "The workload changed recently and is in its cooldown period.",
        "Wait until the cooldown (automation.policy.cooldownMinutes) ends.",
    ),
    "workload_plan_already_exists": (
        "Another plan for this workload is still active.",
        "A new preview reuses or replaces an unapproved one; an approved plan must finish or be cancelled first.",
    ),
    "rollout_in_progress": (
        "The workload has a rollout in progress.",
        "Wait for the rollout to complete.",
    ),
    "controller_unhealthy": (
        "The owning GitOps controller is suspended or unhealthy.",
        "Restore the controller to a healthy, syncing state.",
    ),
    "policy_interaction_conflict": (
        "An HPA, VPA or PodDisruptionBudget conflicts with this change.",
        "Remove the conflicting writer (for example set the VPA to updateMode Off) or apply the change manually.",
    ),
    # Warnings.
    "singleton_workload": (
        "The workload runs a single replica: the rollout restarts its only pod.",
        "Schedule the merge in a maintenance window or add a replica first.",
    ),
    "memory_decrease_best_effort": (
        "A memory decrease takes effect only when the pod restarts.",
        "Expect the saving after the next rollout.",
    ),
    "reliability_change": (
        "This change increases resources to protect reliability; it costs money.",
        "Review the incident risk it avoids against its cost.",
    ),
}


def _entry(code: str, message: Optional[str] = None, fix: Optional[str] = None) -> Dict[str, str]:
    default_message, default_fix = CODES.get(code, ("", ""))
    return {"code": code, "message": message or default_message, "fix": fix or default_fix}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value.value if hasattr(value, "value") else value)


@dataclass(frozen=True)
class EligibilityContext:
    """Live state eligibility depends on, gathered once per request."""

    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: The plan service is composed in this process (a plan could be prepared).
    automation_configured: bool = False
    #: ``AUTOMATION_ENABLED`` and an automation mode other than ``disabled``.
    automation_enabled: bool = False
    policy: AutomationPolicyConfig = field(default_factory=AutomationPolicyConfig)
    policy_error: Optional[str] = None
    #: Freshened bindings by (namespace, kind, name); None when they were not read.
    bindings: Optional[Mapping[Tuple[str, str, str], WorkloadBinding]] = None
    #: Recommendation ids with an open pull request; None when they could not be read.
    open_pr_recommendation_ids: Optional[FrozenSet[int]] = frozenset()
    #: Policy evaluator results by recommendation id (resolved bindings only); None when the
    #: evaluation failed, which blocks the recommendation (fail closed).
    policy_results: Mapping[int, Optional[AutomationPolicyResult]] = field(default_factory=dict)
    step_seconds: int = 300


def workload_key(record: Any) -> Optional[Tuple[str, str, str]]:
    namespace, kind, name = (getattr(record, attr, None) for attr in ("namespace", "owner_kind", "owner_name"))
    if not namespace or not kind or not name:
        return None
    return str(namespace), str(kind), str(name)


def validate_action(record: Any) -> Tuple[Optional[List[int]], Optional[str]]:
    """Server-side check of a typed container-resources action (replaces the UI's ``safeActionFor``).

    Returns ``(recommendation_ids, None)`` for a safe action, otherwise ``(None, blocker_code)``.
    """
    action = getattr(record, "action", None)
    if action is None:
        return None, "action_unavailable"
    data = action.model_dump(mode="json") if hasattr(action, "model_dump") else action
    if not isinstance(data, Mapping):
        return None, "action_unavailable"
    rec_id = getattr(record, "id", None)
    ids = data.get("recommendation_ids")
    raw_target = data.get("target")
    target: Mapping[str, Any] = raw_target if isinstance(raw_target, Mapping) else {}
    operations = data.get("operations")
    container = getattr(record, "container_name", None)
    if (
        not isinstance(rec_id, int)
        or rec_id <= 0
        or data.get("version") != 3
        or not data.get("action_id")
        or not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in ids)
        or len(set(ids)) != len(ids)
        or rec_id not in ids
        or not isinstance(operations, list)
        or not operations
    ):
        return None, "action_unavailable"
    raw_constraints = data.get("constraints")
    constraints: Mapping[str, Any] = raw_constraints if isinstance(raw_constraints, Mapping) else {}
    if (
        constraints.get("container_targeted") is not True
        or getattr(record, "scope", None) != "workload"
        or not container
        or not target.get("api_version")
        or target.get("kind") != getattr(record, "owner_kind", None)
        or target.get("name") != getattr(record, "owner_name", None)
        or target.get("namespace") != getattr(record, "namespace", None)
    ):
        return None, "requires_container_target"
    containers = [op.get("container") if isinstance(op, Mapping) else None for op in operations]
    if len(set(containers)) != len(containers) or container not in containers:
        return None, "requires_container_target"
    for op in operations:
        requests = ((op.get("resources") or {}) if isinstance(op, Mapping) else {}).get("requests")
        if (
            not isinstance(op, Mapping)
            or op.get("op") != "set_container_resources"
            or not isinstance(op.get("container"), str)
            or not op["container"].strip()
            or not isinstance(requests, Mapping)
            or not requests
            or any(
                resource not in ("cpu", "memory") or not isinstance(quantity, str) or not quantity.strip()
                for resource, quantity in requests.items()
            )
        ):
            return None, "requires_container_target"
    return list(ids), None


def warm_up(record: Any, policy: AutomationPolicyConfig, step_seconds: int, now: datetime) -> tuple:
    """Warm-up state from the evidence against the policy minimums (F7).

    Returns ``(blocker or None, eligible_at or None)``. Samples accrue at one
    per query step (a conservative estimate for multi-replica workloads), so
    the minimum sample count and the minimum coverage of the observation
    window are reached after a computable delay from the end of the window.
    """
    evidence = getattr(record, "evidence", None)
    if evidence is None:
        return (
            _entry(
                "policy_evidence_missing",
                "This recommendation has no usage evidence (sample count and coverage).",
                "Wait for the next scan; evidence is collected for workloads with container metrics.",
            ),
            None,
        )
    samples = int(getattr(evidence, "sample_count", 0) or 0)
    coverage = float(getattr(evidence, "coverage_ratio", 0.0) or 0.0)
    if samples >= policy.min_samples and coverage >= policy.min_coverage:
        return None, None

    step = max(int(step_seconds or 300), 1)
    window = getattr(evidence, "observation_window_seconds", None)
    end = getattr(evidence, "observation_window_end", None)
    remaining = max(policy.min_samples - samples, 0) * step
    if coverage < policy.min_coverage and window and math.isfinite(float(window)):
        remaining = max(remaining, (policy.min_coverage - coverage) * float(window))
    eligible_at: Optional[datetime] = None
    if isinstance(end, datetime):
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        eligible_at = end + timedelta(seconds=remaining)

    detail = (
        f"Collecting evidence: {samples}/{policy.min_samples} samples, "
        f"{coverage:.0%} coverage (minimum {policy.min_coverage:.0%})."
    )
    if eligible_at is None:
        fix = "Wait until enough samples are collected; no action is needed."
    elif eligible_at <= now:
        fix = "Enough data should now be available: eligible after the next recommendation scan."
    else:
        hours = max(1, math.ceil((eligible_at - now).total_seconds() / 3600))
        fix = f"Eligible in ~{hours}h (around {eligible_at.isoformat(timespec='minutes')}); no action is needed."
    return _entry("warming_up", detail, fix), eligible_at


def _binding_blocker(binding: Optional[WorkloadBinding]) -> Optional[Dict[str, str]]:
    if binding is None:
        return _entry("binding_missing")
    status = binding.status
    if status is BindingStatus.RESOLVED and binding.source is not None:
        return None
    code = {
        BindingStatus.AMBIGUOUS: "ambiguous_binding",
        BindingStatus.BLOCKED: "binding_blocked",
        BindingStatus.STALE: "binding_stale",
    }.get(status, "binding_unresolved")
    default_message = CODES[code][0]
    message = f"{default_message} ({binding.reason})" if binding.reason else default_message
    return _entry(code, message)


def _local_policy_blockers(record: Any, policy: AutomationPolicyConfig, now: datetime) -> List[Dict[str, str]]:
    blockers: List[Dict[str, str]] = []
    order = {"low": 0, "medium": 1, "high": 2}
    risk = _text(getattr(record, "risk_level", None))
    limit = policy.max_risk_level.value
    if risk not in order:
        blockers.append(_entry("policy_evidence_missing", "The recommendation has no risk level."))
    elif order[risk] > order[limit]:
        blockers.append(
            _entry(
                "policy_risk_exceeded",
                f"Risk {risk} exceeds the automation policy maximum ({limit}).",
            )
        )
    confidence = getattr(record, "confidence", None)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        blockers.append(_entry("policy_evidence_missing", "The recommendation has no confidence score."))
    elif confidence < policy.min_confidence:
        blockers.append(
            _entry(
                "policy_confidence_too_low",
                f"Confidence {confidence:.2f} is below the automation policy minimum ({policy.min_confidence:.2f}).",
            )
        )
    for window in policy.change_freeze_windows:
        if window.start <= now < window.end:
            reason = f" ({window.reason})" if window.reason else ""
            blockers.append(
                _entry(
                    "change_freeze_active",
                    f"A change-freeze window is active until {window.end.isoformat()}{reason}.",
                    "Wait until the freeze window ends.",
                )
            )
            break
    return blockers


def _evaluator_entries(result: AutomationPolicyResult) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Blockers and warnings of a policy evaluation, minus the gates evaluated locally."""
    details = {rejection.code.value: rejection.detail for rejection in result.rejections}
    rejected = [
        (check.gate, check.code, check.message)
        for check in result.checks
        if check.state.value == "rejected" and check.code and check.gate not in _LOCAL_GATES
    ]
    blockers = [
        _entry(code, message, details.get(code))
        for gate, code, message in rejected
        if code and gate not in _WARNING_GATES
    ]
    warnings = [
        _entry(code, message, details.get(code)) for gate, code, message in rejected if code and gate in _WARNING_GATES
    ]
    warnings += [_entry(warning.code.value, warning.detail, warning.detail) for warning in result.warnings]
    return blockers, warnings


def _dedupe(entries: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: set = set()
    result: List[Dict[str, str]] = []
    for entry in entries:
        key = (entry["code"], entry["message"])
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def assess_eligibility(record: Any, ctx: EligibilityContext) -> Dict[str, Any]:
    """Eligibility of one recommendation (record or recommendation) under the live context."""
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    eligible_at: Optional[datetime] = None
    rec_type = _text(getattr(record, "type", None))
    status = _text(getattr(record, "status", None)) or "active"
    rec_id = getattr(record, "id", None)
    active = status == "active"

    open_prs = ctx.open_pr_recommendation_ids
    if status == "pr_open" or (rec_id is not None and open_prs is not None and rec_id in open_prs):
        blockers.append(_entry("pull_request_open"))
    elif not active:
        blockers.append(_entry("not_active", f"The recommendation is {status}; only active ones can become a PR."))

    action_kind = SUPPORTED_ACTION_KINDS.get(rec_type)
    recommendation_ids: Optional[List[int]] = None
    if action_kind is None and rec_type in DELETION_TYPES:
        # Deletions stay manual (no deletion PR is automated in this release, roadmap H6).
        blockers.append(_entry("deletion_requires_review"))
    elif action_kind is None:
        planned = PLANNED_ACTION_KINDS.get(rec_type)
        message = CODES["action_kind_unsupported"][0]
        if planned:
            message = f"{message} The {planned[0]} action kind is planned (roadmap {planned[1]})."
        blockers.append(_entry("action_kind_unsupported", message))
    else:
        recommendation_ids, action_code = validate_action(record)
        if action_code:
            blockers.append(_entry(action_code))

    workload_ready = action_kind is not None and recommendation_ids is not None
    if not ctx.automation_configured:
        blockers.append(_entry("automation_not_configured"))
    elif not ctx.automation_enabled:
        blockers.append(_entry("automation_disabled"))
    elif workload_ready:
        key = workload_key(record)
        if open_prs is None and active:
            blockers.append(_entry("pull_requests_unavailable"))
        if ctx.bindings is None:
            blockers.append(_entry("binding_unavailable"))
        else:
            binding_blocker = _binding_blocker(ctx.bindings.get(key) if key else None)
            if binding_blocker:
                blockers.append(binding_blocker)

    if action_kind is not None:
        if ctx.policy_error:
            blockers.append(_entry("policy_invalid", f"Invalid automation policy: {ctx.policy_error}"))
        else:
            warm_blocker, eligible_at = warm_up(record, ctx.policy, ctx.step_seconds, ctx.now)
            if warm_blocker:
                blockers.append(warm_blocker)
            blockers.extend(_local_policy_blockers(record, ctx.policy, ctx.now))
            result = ctx.policy_results.get(rec_id) if rec_id is not None else None
            if rec_id is not None and rec_id in ctx.policy_results and result is None:
                blockers.append(_entry("policy_evaluation_unavailable"))
            elif result is not None:
                evaluator_blockers, evaluator_warnings = _evaluator_entries(result)
                blockers.extend(evaluator_blockers)
                warnings.extend(evaluator_warnings)

    risk_factors = [str(factor) for factor in getattr(record, "risk_factors", None) or []]
    for code in ("singleton_workload", "memory_decrease_best_effort"):
        if code in risk_factors:
            warnings.append(_entry(code))
    value = getattr(record, "value", None)
    if isinstance(value, Mapping) and value.get("lane") == "reliability":
        warnings.append(_entry("reliability_change"))

    blockers = _dedupe(blockers)
    can_open_pr = not blockers
    delivery_modes = ([DELIVERY_DIRECT] if can_open_pr else []) + ([DELIVERY_MANUAL] if active else [])
    return {
        "can_open_pr": can_open_pr,
        "delivery_modes": delivery_modes,
        "blockers": blockers,
        "warnings": _dedupe(warnings),
        "eligible_at": eligible_at.isoformat() if eligible_at is not None and blockers else None,
        "action_kind": action_kind,
        "recommendation_ids": recommendation_ids if can_open_pr else None,
        "evaluated_at": ctx.now.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Gathering the live context                                                  #
# --------------------------------------------------------------------------- #


def load_policy(raw: Optional[str]) -> Tuple[AutomationPolicyConfig, Optional[str]]:
    """The automation policy, as the plan runtime reads it (``GITOPS_POLICY_JSON``)."""
    try:
        return AutomationPolicyConfig.from_json(raw), None
    except Exception as exc:  # invalid JSON or values: fail closed with an explanation
        return AutomationPolicyConfig(), str(exc).splitlines()[0][:200]


async def load_bindings(
    gitops_repo: Any, cluster: str, now: datetime
) -> Optional[Dict[Tuple[str, str, str], WorkloadBinding]]:
    """Freshened workload bindings of the cluster; None when they cannot be read."""
    try:
        bindings = await gitops_repo.list_workload_bindings(cluster=cluster)
    except Exception as exc:
        logger.warning("Could not read GitOps workload bindings for eligibility: %s", exc)
        return None
    result: Dict[Tuple[str, str, str], WorkloadBinding] = {}
    for binding in bindings or []:
        fresh = freshen_workload_binding(binding, now=now)
        result[(fresh.namespace, fresh.workload_kind, fresh.workload_name)] = fresh
    return result


async def load_open_pr_ids(pr_repo: Any) -> Optional[FrozenSet[int]]:
    """Recommendation ids with an open pull request; None when they cannot be read (fail closed)."""
    if pr_repo is None:
        return frozenset()
    try:
        records = await pr_repo.get_open_pull_requests()
    except Exception as exc:
        logger.warning("Could not read open pull requests for eligibility: %s", exc)
        return None
    ids = set()
    for record in records or []:
        status = _text(getattr(record, "status", None))
        rec_id = getattr(record, "recommendation_id", None)
        if isinstance(rec_id, int) and status in ("open", "pending", ""):
            ids.add(rec_id)
    return frozenset(ids)


async def evaluate_policies(
    records: Sequence[Any],
    ctx: EligibilityContext,
    plan_repository: Any,
) -> Dict[int, Optional[AutomationPolicyResult]]:
    """Run the automation policy evaluator read-only for records whose binding is resolved.

    A record whose evaluation fails maps to None, so its eligibility blocks with
    ``policy_evaluation_unavailable`` instead of skipping the gates.
    """
    if ctx.bindings is None or ctx.policy_error or not (ctx.automation_configured and ctx.automation_enabled):
        return {}
    evaluator = AutomationPolicyEvaluator(config=ctx.policy, plan_repository=plan_repository, clock=lambda: ctx.now)
    results: Dict[int, Optional[AutomationPolicyResult]] = {}
    for record in records:
        rec_id = getattr(record, "id", None)
        key = workload_key(record)
        binding = ctx.bindings.get(key) if key else None
        if (
            rec_id is None
            or binding is None
            or binding.status is not BindingStatus.RESOLVED
            or binding.source is None
            or _text(getattr(record, "type", None)) not in SUPPORTED_ACTION_KINDS
            or _text(getattr(record, "status", None)) not in ("active", "")
            or validate_action(record)[0] is None
        ):
            # Only records that could otherwise become a PR cost a plan-history read.
            continue
        try:
            results[rec_id] = await evaluator.evaluate([record], binding.source)
        except Exception as exc:
            logger.warning("Policy evaluation failed for recommendation %s: %s", rec_id, exc)
            results[rec_id] = None
    return results


def criticality_by_record(records: Sequence[Any], ctx: EligibilityContext) -> Dict[int, Optional[str]]:
    """``greenkube.io/criticality`` of each record's workload, read from its binding evidence."""
    result: Dict[int, Optional[str]] = {}
    if not ctx.bindings:
        return result
    for record in records:
        rec_id = getattr(record, "id", None)
        key = workload_key(record)
        binding = ctx.bindings.get(key) if key else None
        if rec_id is None or binding is None or binding.source is None:
            continue
        criticality = profile_from_binding_evidence(binding.source.evidence).criticality
        if criticality:
            result[rec_id] = criticality
    return result
