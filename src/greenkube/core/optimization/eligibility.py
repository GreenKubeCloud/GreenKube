# src/greenkube/core/optimization/eligibility.py
"""Server-side delivery eligibility of a recommendation (roadmap F10, F7).

Every recommendation says whether GreenKube can open a pull request for it
and, when it cannot, why and what would make it eligible (principle 6, fail
closed, explain why)::

    {"can_open_pr": false,
     "delivery_modes": ["manual"],   # "direct", "patch" or "ci" first when a PR can be opened
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
ACTION_DELETE_RESOURCE = "delete_resource"

#: Recommendation types GreenKube can turn into a pull request, by action kind.
SUPPORTED_ACTION_KINDS: Dict[str, str] = {
    "RIGHTSIZING_CPU": ACTION_CONTAINER_RESOURCES,
    "RIGHTSIZING_MEMORY": ACTION_CONTAINER_RESOURCES,
    # Orphaned LoadBalancers and PersistentVolumes become reviewed deletion PRs (H6);
    # zombie workloads and idle namespaces stay manual (deletion_requires_review).
    "ORPHANED_LOAD_BALANCER": ACTION_DELETE_RESOURCE,
    "ORPHANED_PERSISTENT_VOLUME": ACTION_DELETE_RESOURCE,
}

#: Action kinds planned for the other types (roadmap H), named in the blocker.
PLANNED_ACTION_KINDS: Dict[str, Tuple[str, str]] = {
    "AUTOSCALING_CANDIDATE": ("hpa", "H3"),
    "OFF_PEAK_SCALING": ("schedule", "H4"),
    "ZOMBIE_POD": ("deletion", "H6"),
    "IDLE_NAMESPACE": ("deletion", "H6"),
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
    "deletion_reviewed_in_pr": (
        "A deletion pull request is never approved automatically: GreenKube shows why the resource appears "
        "unused, a person confirms it.",
        "Check the evidence in the recommendation and with the owners before approving and merging the pull "
        'request. Annotate the resource with greenkube.io/ignore="true" if it is still needed.',
    ),
    "deletion_prune_disabled": (
        "The GitOps controller managing this resource does not prune, so removing it from Git will not delete it.",
        "Enable Argo CD syncPolicy.automated.prune or Flux spec.prune, or delete the live object by hand after "
        "the merge.",
    ),
    "resource_binding_missing": (
        "No Argo CD Application or Flux Kustomization lists this resource, so GreenKube cannot find it in Git.",
        "Delete it by hand (kubectl delete, see the manual snippet) and mark the recommendation as applied, or "
        "manage it with GitOps and refresh discovery.",
    ),
    "resource_binding_unavailable": (
        "The resource bindings of the GitOps controllers could not be read.",
        "Check the database connection and GET /api/v1/gitops/status, then retry.",
    ),
    "no_effective_change": (
        "The proposed request equals the current one, so a pull request would change nothing.",
        "No action is needed: the workload already runs this request. The next scan resolves the recommendation.",
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
        "The automation plan service is not available, so no pull request can be prepared.",
        "Open the setup checklist (GET /api/v1/setup/status) and check the API logs.",
    ),
    "automation_disabled": (
        "Pull-request delivery is paused (automation.policy.pauseDelivery).",
        "Set automation.policy.pauseDelivery=false once the change freeze is over.",
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
    "consensus_contested": (
        "Sources disagree on this change: it is never approved automatically.",
        "Review the per-source spread in the evidence before opening the pull request.",
    ),
}


def _configured_delivery_mode() -> str:
    from greenkube.automation.delivery import configured_delivery_mode

    try:
        return configured_delivery_mode()
    except Exception:  # configuration errors surface elsewhere; eligibility stays readable
        return DELIVERY_DIRECT


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
    #: Delivery is not paused (``automation.policy.pauseDelivery``, F12).
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
    #: Delivery mode of new approvals (F4): ``direct``, ``patch`` or ``ci``.
    delivery_mode: str = field(default_factory=lambda: _configured_delivery_mode())
    #: Resource bindings (H0d) by (kind, namespace, name); None when they were not read.
    resource_bindings: Optional[Mapping[Tuple[str, str, str], Any]] = None


def workload_key(record: Any) -> Optional[Tuple[str, str, str]]:
    namespace, kind, name = (getattr(record, attr, None) for attr in ("namespace", "owner_kind", "owner_name"))
    if not namespace or not kind or not name:
        return None
    return str(namespace), str(kind), str(name)


def proposes_no_change(record: Any) -> bool:
    """True when a rightsizing proposal equals the current request after unit normalization.

    Compares ``recommended_*`` with ``current_*`` and, when the record carries
    a typed action, the action's quantity for its container (``1Gi`` equals
    ``1024Mi``, ``0.5`` equals ``500m``), so a no-op never becomes a PR.
    """
    from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

    rec_type = _text(getattr(record, "type", None))
    if rec_type == "RIGHTSIZING_CPU":
        resource, parser = "cpu", parse_cpu_quantity
        current = getattr(record, "current_cpu_request_millicores", None)
        recommended = getattr(record, "recommended_cpu_request_millicores", None)
    elif rec_type == "RIGHTSIZING_MEMORY":
        resource, parser = "memory", parse_memory_quantity
        current = getattr(record, "current_memory_request_bytes", None)
        recommended = getattr(record, "recommended_memory_request_bytes", None)
    else:
        return False
    if current is None or recommended is None:
        return False
    if current == recommended:
        return True
    action = getattr(record, "action", None)
    data = action.model_dump(mode="json") if hasattr(action, "model_dump") else action
    container = getattr(record, "container_name", None)
    if not isinstance(data, Mapping) or not container:
        return False
    for operation in data.get("operations") or []:
        if not isinstance(operation, Mapping) or operation.get("container") != container:
            continue
        resources = operation.get("resources")
        requests = resources.get("requests") if isinstance(resources, Mapping) else None
        quantity = requests.get(resource) if isinstance(requests, Mapping) else None
        if quantity is None:
            continue
        try:
            return parser(quantity) == current
        except Exception:
            return False
    return False


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
        resources = ((op.get("resources") or {}) if isinstance(op, Mapping) else {}) or {}
        requests = resources.get("requests") if isinstance(resources, Mapping) else None
        limits = resources.get("limits", {}) if isinstance(resources, Mapping) else None
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
            or not _valid_limit_edits(op, limits)
        ):
            return None, "requires_container_target"
    return list(ids), None


def _valid_limit_edits(op: Mapping[str, Any], limits: Any) -> bool:
    """Container resources v2 (H1): CPU/memory limits, a removed CPU limit and a known container list."""
    removals = op.get("remove_limits", [])
    if not isinstance(limits, Mapping) or not isinstance(removals, list):
        return False
    if any(
        resource not in ("cpu", "memory") or not isinstance(quantity, str) or not quantity.strip()
        for resource, quantity in limits.items()
    ):
        return False
    if any(resource != "cpu" for resource in removals) or set(removals) & set(limits):
        return False
    return op.get("container_kind", "container") in ("container", "sidecar", "init")


def validate_deletion_action(record: Any) -> Tuple[Optional[List[int]], Optional[str]]:
    """Server-side check of a typed ``delete_resource`` action: it deletes this recommendation's resource."""
    from greenkube.automation.actions.delete_resource import deletion_target_for

    action = getattr(record, "action", None)
    data = action.model_dump(mode="json") if hasattr(action, "model_dump") else action
    target = deletion_target_for(record)
    rec_id = getattr(record, "id", None)
    if not isinstance(data, Mapping) or target is None or not isinstance(rec_id, int):
        return None, "action_unavailable"
    ids = data.get("recommendation_ids")
    operations = data.get("operations")
    if (
        data.get("version") != 3
        or not isinstance(ids, list)
        or rec_id not in ids
        or not isinstance(operations, list)
        or [op.get("op") if isinstance(op, Mapping) else None for op in operations] != ["delete_resource"]
        or data.get("target") != target.model_dump(mode="json")
    ):
        return None, "action_unavailable"
    return list(ids), None


def resource_key(record: Any) -> Optional[Tuple[str, str, str]]:
    """``(kind, namespace, name)`` of the resource a deletion recommendation targets."""
    from greenkube.automation.actions.delete_resource import deletion_target_for

    target = deletion_target_for(record)
    return (target.kind, target.namespace, target.name) if target is not None else None


def _resource_binding_entries(
    record: Any, ctx: "EligibilityContext"
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Blockers and warnings from the resource binding of a deletion (H0d, H6)."""
    if ctx.resource_bindings is None:
        return [_entry("resource_binding_unavailable")], []
    key = resource_key(record)
    stored = ctx.resource_bindings.get(key) if key else None
    binding = getattr(stored, "binding", stored)
    if binding is None:
        return [_entry("resource_binding_missing")], []
    if getattr(binding, "status", None) == "ambiguous":
        return [_entry("ambiguous_binding")], []
    observed_at = getattr(stored, "observed_at", None)
    if not getattr(binding, "resolved", False):
        return [_entry("binding_unresolved", f"{CODES['binding_unresolved'][0]} ({binding.reason})")], []
    if isinstance(observed_at, datetime) and ctx.now - observed_at >= _binding_ttl():
        return [_entry("binding_stale")], []
    warnings = [] if getattr(binding, "prune", None) is True else [_entry("deletion_prune_disabled")]
    return [], warnings


def _binding_ttl() -> timedelta:
    from greenkube.gitops.contracts import configured_binding_ttl

    return configured_binding_ttl()


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
    deletion = action_kind == ACTION_DELETE_RESOURCE
    if deletion:
        recommendation_ids, action_code = validate_deletion_action(record)
        if action_code:
            blockers.append(_entry(action_code))
        # A person always reviews a deletion: the PR is offered, never auto-approved.
        warnings.append(_entry("deletion_reviewed_in_pr"))
    elif action_kind is None and rec_type in DELETION_TYPES:
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
        if proposes_no_change(record):
            blockers.append(_entry("no_effective_change"))

    workload_ready = action_kind is not None and recommendation_ids is not None
    if not ctx.automation_configured:
        blockers.append(_entry("automation_not_configured"))
    elif not ctx.automation_enabled:
        blockers.append(_entry("automation_disabled"))
    elif workload_ready and deletion:
        if open_prs is None and active:
            blockers.append(_entry("pull_requests_unavailable"))
        binding_blockers, binding_warnings = _resource_binding_entries(record, ctx)
        blockers.extend(binding_blockers)
        warnings.extend(binding_warnings)
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
            # A deletion's evidence is the minimum unused period, already required by its analyzer.
            warm_blocker, eligible_at = (
                (None, None) if deletion else warm_up(record, ctx.policy, ctx.step_seconds, ctx.now)
            )
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
    consensus = getattr(record, "consensus", None)
    if isinstance(consensus, Mapping) and consensus.get("contested"):
        # Contested advice blocks auto-approval (E6); a human can still open the PR.
        reasons = "; ".join(str(reason) for reason in consensus.get("reasons") or [])
        message = (
            f"Sources disagree on this change ({reasons}): it is never approved automatically." if reasons else None
        )
        warnings.append(_entry("consensus_contested", message))

    blockers = _dedupe(blockers)
    can_open_pr = not blockers
    delivery_modes = ([ctx.delivery_mode or DELIVERY_DIRECT] if can_open_pr else []) + (
        [DELIVERY_MANUAL] if active else []
    )
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


async def load_resource_bindings(gitops_repo: Any, cluster: str) -> Optional[Dict[Tuple[str, str, str], Any]]:
    """Persisted resource bindings (H0d) by (kind, namespace, name); None when unreadable (fail closed)."""
    db_manager = getattr(gitops_repo, "db_manager", None)
    if db_manager is None:
        return None
    from greenkube.storage.resource_binding_repository import ResourceBindingRepository

    try:
        stored = await ResourceBindingRepository(db_manager).list_cluster(cluster)
    except Exception as exc:
        logger.warning("Could not read GitOps resource bindings for eligibility: %s", exc)
        return None
    return {(item.binding.ref.kind, item.binding.ref.namespace, item.binding.ref.name): item for item in stored}


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


# --------------------------------------------------------------------------- #
# Policy-based auto-approval (roadmap F5)                                      #
# --------------------------------------------------------------------------- #

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
#: Why ``automation.autoApprove`` left a preview for a human (docs/automation.md#auto-approval).
AUTO_APPROVE_CODES: Dict[str, str] = {
    "auto_approve_disabled": "automation.autoApprove is off.",
    "approve_scope_missing": "The requester may not approve plans (approve scope).",
    "plan_not_validated": "Only a validated, render-checked preview can be approved automatically.",
    "rollback_plan": "Rollbacks are always approved by a person.",
    "recommendation_missing": "A recommendation of the plan could not be read.",
    "not_active": "A recommendation of the plan is not active.",
    "risk_above_auto_approve": "A recommendation's risk is above automation.autoApprove.maxRisk.",
    "confidence_below_auto_approve": "A recommendation's confidence is below automation.autoApprove.minConfidence.",
    "consensus_contested": "Sources disagree on a recommendation: contested advice is never approved automatically.",
    "deletion_requires_review": "A deletion is always approved by a person.",
    "decrease_without_native_evidence": (
        "A decrease needs GreenKube's own usage evidence or at least two independent agreeing sources."
    ),
    "increase_requires_approval": (
        "A request increase is a reliability fix that costs money; set automation.autoApprove.increases to "
        "approve increases automatically."
    ),
    "increase_without_native_evidence": (
        "A request increase is only approved automatically when GreenKube's own analyzer sized it "
        "(pod-bounded usage, increase cap, warm-up excluded)."
    ),
}


@dataclass(frozen=True)
class AutoApprovePolicy:
    """``automation.autoApprove`` (F5): previews meeting these limits skip the extra click.

    Off by default. Contested consensus is a hard stop whatever the settings, and a
    request decrease also needs native evidence or two independent consensus votes.
    """

    enabled: bool = False
    max_risk: str = "low"
    min_confidence: float = 0.8
    #: Also approve request increases (H1, ``automation.autoApprove.increases``).
    increases: bool = False

    @classmethod
    def from_config(cls, config: Any) -> "AutoApprovePolicy":
        risk = str(getattr(config, "AUTOMATION_AUTO_APPROVE_MAX_RISK", "low") or "low").lower()
        try:
            confidence = float(getattr(config, "AUTOMATION_AUTO_APPROVE_MIN_CONFIDENCE", 0.8))
        except (TypeError, ValueError):
            confidence = 1.0
        return cls(
            enabled=bool(getattr(config, "AUTOMATION_AUTO_APPROVE_ENABLED", False)),
            max_risk=risk if risk in _RISK_ORDER else "low",
            min_confidence=confidence if math.isfinite(confidence) else 1.0,
            increases=getattr(config, "AUTOMATION_AUTO_APPROVE_INCREASES", False) is True,
        )


def is_contested(record: Any) -> bool:
    """Contested consensus (E6): ``consensus.contested`` or a ``consensus_contested`` eligibility entry."""
    consensus = getattr(record, "consensus", None)
    if isinstance(consensus, Mapping) and consensus.get("contested"):
        return True
    if getattr(consensus, "contested", None) is True:
        return True
    eligibility = getattr(record, "eligibility", None)
    if isinstance(eligibility, Mapping):
        for entry in [*(eligibility.get("warnings") or []), *(eligibility.get("blockers") or [])]:
            if isinstance(entry, Mapping) and entry.get("code") == "consensus_contested":
                return True
    return False


def proposes_decrease(record: Any) -> bool:
    """True when a proposal lowers the current CPU or memory request (unknown counts as a decrease)."""
    rec_type = _text(getattr(record, "type", None))
    if rec_type == "RIGHTSIZING_CPU":
        current = getattr(record, "current_cpu_request_millicores", None)
        recommended = getattr(record, "recommended_cpu_request_millicores", None)
    elif rec_type == "RIGHTSIZING_MEMORY":
        current = getattr(record, "current_memory_request_bytes", None)
        recommended = getattr(record, "recommended_memory_request_bytes", None)
    else:
        return True
    if not isinstance(current, (int, float)) or not isinstance(recommended, (int, float)):
        return True
    return recommended < current


def proposes_increase(record: Any) -> bool:
    """True when a proposal raises the current CPU or memory request (a reliability fix, H1)."""
    rec_type = _text(getattr(record, "type", None))
    if rec_type == "RIGHTSIZING_CPU":
        current = getattr(record, "current_cpu_request_millicores", None)
        recommended = getattr(record, "recommended_cpu_request_millicores", None)
    elif rec_type == "RIGHTSIZING_MEMORY":
        current = getattr(record, "current_memory_request_bytes", None)
        recommended = getattr(record, "recommended_memory_request_bytes", None)
    else:
        return False
    if not isinstance(current, (int, float)) or not isinstance(recommended, (int, float)):
        return False
    return recommended > current


def has_native_evidence(record: Any) -> bool:
    """GreenKube's own analyzers chose the proposal and its usage evidence has samples."""
    evidence = getattr(record, "evidence", None)
    if evidence is None or int(getattr(evidence, "sample_count", 0) or 0) <= 0:
        return False
    source = _text(getattr(record, "source", None)) or "greenkube"
    if source == "greenkube" or source.startswith("greenkube."):
        return True
    consensus = getattr(record, "consensus", None)
    chosen = consensus.get("chosen_from") if isinstance(consensus, Mapping) else getattr(consensus, "chosen_from", None)
    return any(str(item) == "greenkube" or str(item).startswith("greenkube.") for item in chosen or [])


def independent_votes(record: Any) -> int:
    """Independent consensus votes behind a recommendation (one per engine family), 1 without consensus."""
    consensus = getattr(record, "consensus", None)
    if isinstance(consensus, Mapping):
        value = consensus.get("independent_votes")
    else:
        value = getattr(consensus, "independent_votes", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _auto_approvable(record: Any) -> bool:
    """Whether the record's action kind allows policy-based auto-approval (``ActionKindSpec.auto_approvable``)."""
    from greenkube.automation.actions import action_kind_of

    action = getattr(record, "action", None)
    spec = action_kind_of(action) if action is not None and hasattr(action, "operations") else None
    return spec is None or spec.auto_approvable


def auto_approve_blockers(records: Sequence[Any], policy: AutoApprovePolicy, *, expected: int) -> List[str]:
    """Why ``policy`` does not approve a preview of ``records`` automatically (empty: approve it).

    ``expected`` is the number of recommendations of the plan: a recommendation that
    could not be read blocks (fail closed).
    """
    if not policy.enabled:
        return ["auto_approve_disabled"]
    blockers: List[str] = []
    if any(
        _text(getattr(record, "type", None)) in DELETION_TYPES or not _auto_approvable(record) for record in records
    ):
        # Deletions are never approved automatically, whatever the limits (H6).
        blockers.append("deletion_requires_review")
    if expected <= 0 or len(records) != expected:
        blockers.append("recommendation_missing")
    limit = _RISK_ORDER.get(policy.max_risk, 0)
    for record in records:
        status = _text(getattr(record, "status", None)) or "active"
        if status != "active":
            blockers.append("not_active")
        risk = _text(getattr(record, "risk_level", None))
        if risk not in _RISK_ORDER or _RISK_ORDER[risk] > limit:
            blockers.append("risk_above_auto_approve")
        confidence = getattr(record, "confidence", None)
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(float(confidence))
            or confidence < policy.min_confidence
        ):
            blockers.append("confidence_below_auto_approve")
        if is_contested(record):
            blockers.append("consensus_contested")
        if proposes_decrease(record) and not has_native_evidence(record) and independent_votes(record) < 2:
            blockers.append("decrease_without_native_evidence")
        if proposes_increase(record):
            if not policy.increases:
                blockers.append("increase_requires_approval")
            if not has_native_evidence(record):
                blockers.append("increase_without_native_evidence")
    return list(dict.fromkeys(blockers))
