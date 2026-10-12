"""Database row mappers for GitOps persistence."""

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ControllerKind,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    PullRequestEvent,
    RendererKind,
    ValidationResult,
    WorkloadSource,
)
from greenkube.storage.base_gitops_repository import (
    AutomationPlanRecord,
    GitOpsProviderConfig,
    WebhookDelivery,
    WorkerHeartbeat,
    WorkloadBinding,
    WorkloadBindingEvent,
)

PLAN_PRE_APPLY_BASELINE_POLICY_KEY = "pre_apply_baseline"


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _integer_value(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def json_value(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def database_datetime(value: datetime | None, *, postgres: bool) -> datetime | str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if postgres:
        return normalized
    return normalized.isoformat().replace("+00:00", "Z")


def row_to_gitops_provider(row: Any) -> GitOpsProviderConfig:
    return GitOpsProviderConfig(
        id=str(_get(row, "id", "")),
        provider_type=str(_get(row, "provider_type", "")),
        api_base_url=_get(row, "api_base_url"),
        secret_namespace=_get(row, "secret_namespace"),
        secret_name=_get(row, "secret_name"),
        secret_key=_get(row, "secret_key"),
        enabled=bool(_get(row, "enabled", False)),
        capabilities=json_value(_get(row, "capabilities"), {}),
        status=str(_get(row, "status", "unverified")),
        last_checked_at=datetime_value(_get(row, "last_checked_at")),
        last_error_code=_get(row, "last_error_code"),
        created_at=datetime_value(_get(row, "created_at")),
        updated_at=datetime_value(_get(row, "updated_at")),
    )


def row_to_workload_source(
    source_row: Any,
    *,
    cluster: str,
    namespace: str,
    workload_kind: str,
    workload_name: str,
    binding_row: Any,
) -> WorkloadSource:
    return WorkloadSource(
        id=str(_get(source_row, "id", "")),
        cluster=cluster,
        namespace=namespace,
        workload_kind=workload_kind,
        workload_name=workload_name,
        provider_id=str(_get(source_row, "provider_id", "")),
        repo_url=str(_get(source_row, "repo_url", "")),
        ref=str(_get(source_row, "ref", "")),
        revision=_get(source_row, "revision"),
        path=str(_get(source_row, "path", "")),
        controller_kind=ControllerKind(_get(source_row, "controller_kind")),
        controller_name=str(_get(source_row, "controller_name", "")),
        controller_namespace=str(_get(source_row, "controller_namespace", "")),
        controller_uid=_get(source_row, "controller_uid"),
        controller_generation=_integer_value(_get(source_row, "controller_generation")),
        source_uid=_get(source_row, "source_uid"),
        source_generation=_integer_value(_get(source_row, "source_generation")),
        source_revision=_get(source_row, "source_revision"),
        renderer=RendererKind(_get(source_row, "renderer")),
        renderer_config=json_value(_get(source_row, "renderer_config"), {}),
        confidence=float(_get(binding_row, "confidence", 0)),
        status=BindingStatus(_get(binding_row, "status", BindingStatus.UNRESOLVED.value)),
        evidence=json_value(_get(binding_row, "evidence"), {}),
        observed_at=datetime_value(_get(binding_row, "observed_at")) or datetime.now(timezone.utc),
        expires_at=datetime_value(_get(binding_row, "expires_at")),
        reason=_get(binding_row, "reason"),
    )


def row_to_workload_binding_event(row: Any) -> WorkloadBindingEvent:
    return WorkloadBindingEvent(
        id=int(_get(row, "id")),
        cluster=str(_get(row, "cluster", "")),
        namespace=str(_get(row, "namespace", "")),
        workload_kind=str(_get(row, "workload_kind", "")),
        workload_name=str(_get(row, "workload_name", "")),
        event_type=str(_get(row, "event_type", "")),
        actor=str(_get(row, "actor", "")),
        source_id=_get(row, "source_id"),
        source_generation=_get(row, "source_generation"),
        expires_at=datetime_value(_get(row, "expires_at")),
        details=json_value(_get(row, "details"), {}),
        created_at=datetime_value(_get(row, "created_at")) or datetime.now(timezone.utc),
    )


def row_to_workload_binding(
    row: Any,
    *,
    source: WorkloadSource | None,
    candidates: tuple[WorkloadSource, ...],
    candidates_truncated: bool = False,
) -> WorkloadBinding:
    return WorkloadBinding(
        cluster=str(_get(row, "cluster", "")),
        namespace=str(_get(row, "namespace", "")),
        workload_kind=str(_get(row, "workload_kind", "")),
        workload_name=str(_get(row, "workload_name", "")),
        status=BindingStatus(_get(row, "status", BindingStatus.UNRESOLVED.value)),
        confidence=float(_get(row, "confidence", 0)),
        source=source,
        candidates=candidates,
        evidence=json_value(_get(row, "evidence"), {}),
        reason=_get(row, "reason"),
        observed_at=datetime_value(_get(row, "observed_at")) or datetime.now(timezone.utc),
        expires_at=datetime_value(_get(row, "expires_at")),
        generation=_integer_value(_get(row, "generation")) or 0,
        candidates_truncated=candidates_truncated,
    )


def row_to_automation_plan_record(
    row: Any,
    *,
    file_rows: list[Any],
    recommendation_ids: list[int],
) -> AutomationPlanRecord:
    source_data = json_value(_get(row, "source_snapshot"), {})
    action_data = json_value(_get(row, "action_json"), {})
    policy_result = json_value(_get(row, "policy_result"), {})
    if not isinstance(policy_result, dict):
        policy_result = {}
    else:
        policy_result = dict(policy_result)
    baseline_data = policy_result.pop(PLAN_PRE_APPLY_BASELINE_POLICY_KEY, None)
    baseline = PreApplyWorkloadBaseline.model_validate(baseline_data) if baseline_data is not None else None
    source = WorkloadSource.model_validate(source_data)
    action = ActionPlanV3.model_validate(action_data)
    if recommendation_ids and set(recommendation_ids) != set(action.recommendation_ids):
        raise ValueError("plan recommendation links do not match its action contract")
    resolved_recommendation_ids = action.recommendation_ids
    file_plan: FilePlan | None = None
    file_digest = _get(row, "digest")
    if (
        file_rows
        and _get(row, "base_revision")
        and _get(row, "renderer")
        and _get(row, "validation") is not None
        and file_digest
    ):
        validation = ValidationResult.model_validate(json_value(_get(row, "validation"), {}))
        edits = [
            FileEdit(
                path=str(_get(file_row, "path", "")),
                operation=_get(file_row, "operation"),
                original_sha=_get(file_row, "original_sha"),
                content=_get(file_row, "content"),
            )
            for file_row in file_rows
        ]
        file_plan = FilePlan(
            plan_id=str(_get(row, "plan_id", "")),
            files=edits,
            diff=str(_get(row, "diff", "")),
            rendered_diff=str(_get(row, "rendered_diff", "")),
            base_revision=str(_get(row, "base_revision", "")),
            source_revision=_get(row, "source_revision"),
            renderer=RendererKind(_get(row, "renderer")),
            validation=validation,
            warnings=json_value(_get(row, "warnings"), []),
            digest=str(file_digest),
        )
    plan = Plan(
        plan_id=str(_get(row, "plan_id", "")),
        recommendation_ids=resolved_recommendation_ids,
        source=source,
        action=action,
        file_plan=file_plan,
        status=PlanStatus(_get(row, "status", PlanStatus.DRAFT.value)),
        digest=file_digest,
        change_set_id=_get(row, "change_set_id"),
        rollback_of_plan_id=_get(row, "rollback_of_plan_id"),
        policy_result=policy_result,
        pre_apply_baseline=baseline,
        created_at=datetime_value(_get(row, "created_at")) or datetime.now(timezone.utc),
        expires_at=datetime_value(_get(row, "expires_at")),
    )
    return AutomationPlanRecord(
        plan=plan,
        policy_result=policy_result,
        updated_at=datetime_value(_get(row, "updated_at")),
    )


def row_to_webhook_delivery(row: Any) -> WebhookDelivery:
    event: PullRequestEvent | None = None
    if _get(row, "pr_number") is not None:
        try:
            event = PullRequestEvent(
                provider_id=str(_get(row, "provider_id", "")),
                delivery_id=str(_get(row, "delivery_id", "")),
                repo=str(_get(row, "repo", "")),
                number=int(_get(row, "pr_number")),
                action=_get(row, "action"),
                merged=bool(_get(row, "merged", False)),
                merge_commit_sha=_get(row, "merge_commit_sha"),
                head_sha=_get(row, "head_sha"),
                base_sha=_get(row, "base_sha"),
                occurred_at=datetime_value(_get(row, "occurred_at")) or datetime.now(timezone.utc),
            )
        except (TypeError, ValueError, ValidationError):
            event = None
    return WebhookDelivery(
        id=int(_get(row, "id", 0)),
        event_type=str(_get(row, "event_type", "")),
        event=event,
        status=str(_get(row, "status", "received")),
        received_at=datetime_value(_get(row, "received_at")) or datetime.now(timezone.utc),
        processed_at=datetime_value(_get(row, "processed_at")),
        error_code=_get(row, "error_code"),
        created_at=datetime_value(_get(row, "created_at")),
        updated_at=datetime_value(_get(row, "updated_at")),
        provider_id=_get(row, "provider_id"),
        delivery_id=_get(row, "delivery_id"),
    )


def row_to_worker_heartbeat(row: Any) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        worker_id=str(_get(row, "worker_id", "")),
        status=str(_get(row, "status", "starting")),
        heartbeat_at=datetime_value(_get(row, "heartbeat_at")) or datetime.now(timezone.utc),
        lease_id=_get(row, "lease_id"),
        lease_owner=_get(row, "lease_owner"),
        lease_expires_at=datetime_value(_get(row, "lease_expires_at")),
        lease_operation_id=_get(row, "lease_operation_id"),
        lease_plan_operation_id=_get(row, "lease_plan_operation_id"),
        metadata=json_value(_get(row, "metadata"), {}),
        created_at=datetime_value(_get(row, "created_at")),
        updated_at=datetime_value(_get(row, "updated_at")),
    )
