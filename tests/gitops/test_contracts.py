import pytest
from pydantic import ValidationError

from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    AutomationErrorCode,
    BindingResolution,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Operation,
    OperationStatus,
    Plan,
    PlanStatus,
    PullRequestEvent,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
    canonical_repository_identity,
    sanitize_repository_url,
)


def _source(**overrides) -> WorkloadSource:
    data = {
        "id": "src-1",
        "cluster": "prod",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "api",
        "provider_id": "github-main",
        "repo_url": "https://github.com/acme/gitops.git",
        "ref": "main",
        "revision": "a" * 40,
        "path": "apps/payments",
        "controller_kind": ControllerKind.ARGOCD,
        "controller_name": "payments-prod",
        "controller_namespace": "argocd",
        "source_revision": "a" * 40,
        "renderer": RendererKind.RAW,
        "renderer_config": {},
        "confidence": 0.98,
        "status": BindingStatus.RESOLVED,
        "evidence": {},
    }
    data.update(overrides)
    return WorkloadSource(**data)


def test_oci_repository_identity_preserves_distinct_paths_without_credentials():
    first = "oci://user:secret@REGISTRY.example:5443/acme/payments?token=private"
    second = "oci://registry.example:5443/acme/other"

    assert canonical_repository_identity(first) == "oci://registry.example:5443/acme/payments"
    assert sanitize_repository_url(first) == "oci://registry.example:5443/acme/payments"
    assert canonical_repository_identity(first) != canonical_repository_identity(second)


def _action(**overrides) -> ActionPlanV3:
    data = {
        "action_id": "act-1",
        "recommendation_ids": [42],
        "target": ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="api",
        ),
        "operations": [
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m", "memory": "256Mi"}),
            )
        ],
        "constraints": {},
        "evidence_ref": "recommendation/42",
    }
    data.update(overrides)
    return ActionPlanV3(**data)


def _file_plan(**overrides) -> FilePlan:
    data = {
        "plan_id": "plan-1",
        "files": [
            FileEdit(
                path="apps/payments/deployment.yaml",
                operation="update",
                original_sha="blob-1",
                content="apiVersion: apps/v1\nkind: Deployment\n",
            )
        ],
        "diff": "",
        "rendered_diff": "",
        "base_revision": "a" * 40,
        "source_revision": "a" * 40,
        "renderer": RendererKind.RAW,
        "validation": ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest="sha256:" + "b" * 64,
        ),
        "warnings": [],
        "digest": "sha256:" + "c" * 64,
    }
    data.update(overrides)
    return FilePlan(**data)


def test_workload_source_and_binding_resolution_preserve_controller_provenance():
    source = _source()

    resolution = BindingResolution(status=BindingStatus.RESOLVED, binding=source)

    assert resolution.binding is not None
    assert resolution.binding.controller_name == "payments-prod"
    assert resolution.binding.renderer is RendererKind.RAW


def test_binding_resolution_requires_a_binding_only_when_resolved():
    with pytest.raises(ValidationError, match="resolved"):
        BindingResolution(status=BindingStatus.RESOLVED)

    assert BindingResolution(status=BindingStatus.AMBIGUOUS, candidates=[_source()]).binding is None


def test_action_plan_targets_a_named_container_and_accepts_kubernetes_quantities():
    action = _action()

    assert action.version == 3
    operation = action.operations[0]
    assert isinstance(operation, SetContainerResources)
    assert operation.container == "api"
    assert operation.resources.requests == {"cpu": "300m", "memory": "256Mi"}


@pytest.mark.parametrize("container", ["", "  "])
def test_resource_action_rejects_missing_container(container):
    with pytest.raises(ValidationError):
        _action(
            operations=[
                {
                    "op": "set_container_resources",
                    "container": container,
                    "resources": {"requests": {"cpu": "300m"}},
                }
            ]
        )


@pytest.mark.parametrize("quantity", ["3mCPU", "256Mi "])
def test_resource_action_rejects_noncanonical_quantities(quantity):
    with pytest.raises(ValidationError):
        _action(
            operations=[
                {
                    "op": "set_container_resources",
                    "container": "api",
                    "resources": {"requests": {"cpu": quantity}},
                }
            ]
        )


def test_resource_action_rejects_numeric_quantities():
    with pytest.raises(ValidationError):
        _action(
            operations=[
                {
                    "op": "set_container_resources",
                    "container": "api",
                    "resources": {"requests": {"cpu": 300}},
                }
            ]
        )


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret.yaml", "apps/../../secret.yaml", r"apps\secret.yaml"])
def test_file_edit_rejects_paths_outside_the_repository(path):
    with pytest.raises(ValidationError):
        FileEdit(path=path, operation="update", original_sha="blob-1", content="safe")


def test_file_edit_requires_content_for_create_and_update_but_not_delete():
    with pytest.raises(ValidationError):
        FileEdit(path="apps/api.yaml", operation="update", original_sha="blob-1")

    with pytest.raises(ValidationError):
        FileEdit(path="apps/api.yaml", operation="delete", original_sha="blob-1", content="unexpected")

    deletion = FileEdit(path="apps/api.yaml", operation="delete", original_sha="blob-1")
    assert deletion.content is None


def test_file_plan_rejects_duplicate_paths_and_invalid_digest():
    file_edit = FileEdit(
        path="apps/api.yaml",
        operation="update",
        original_sha="blob-1",
        content="kind: Deployment\n",
    )
    with pytest.raises(ValidationError, match="unique"):
        _file_plan(files=[file_edit, file_edit])

    with pytest.raises(ValidationError, match="digest"):
        _file_plan(digest="not-a-digest")


def test_validated_plan_must_include_a_successfully_validated_file_plan():
    action = _action()
    source = _source()
    file_plan = _file_plan()
    plan = Plan(
        plan_id="plan-1",
        recommendation_ids=[42],
        source=source,
        action=action,
        file_plan=file_plan,
        status=PlanStatus.VALIDATED,
        digest=file_plan.digest,
    )

    assert plan.status is PlanStatus.VALIDATED

    with pytest.raises(ValidationError, match="validation"):
        Plan(
            plan_id="plan-2",
            recommendation_ids=[42],
            source=source,
            action=action,
            file_plan=_file_plan(
                validation=ValidationResult(
                    target_changed=True,
                    unrelated_changes=True,
                    requests_within_limits=True,
                    renderer=RendererKind.RAW,
                    output_digest="sha256:" + "b" * 64,
                )
            ),
            status=PlanStatus.VALIDATED,
            digest="sha256:" + "c" * 64,
        )


def test_operation_and_webhook_event_have_typed_lifecycle_fields():
    operation = Operation(
        operation_id="op-1",
        plan_id="plan-1",
        idempotency_key="plan-1",
        status=OperationStatus.QUEUED,
    )
    event = PullRequestEvent(
        provider_id="github-main",
        delivery_id="delivery-1",
        repo="acme/gitops",
        number=12,
        action="closed",
        merged=False,
    )

    assert operation.status is OperationStatus.QUEUED
    assert event.merged is False
    assert ActionStatus.PLAN_READY.value == "plan_ready"
    assert AutomationErrorCode.PLAN_STALE.value == "plan_stale"
    assert AutomationErrorCode.REQUIRES_CONTAINER_TARGET.value == "requires_container_target"
