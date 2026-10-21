import hashlib
import json
from collections.abc import MutableMapping
from dataclasses import replace
from typing import cast

import pytest
from pydantic import ValidationError

from greenkube.gitops.contracts import (
    ActionPlanV3,
    AddHorizontalPodAutoscaler,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    DeleteResource,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.planning import (
    RawManifestPlanError,
    RawManifestPlanner,
    RepositorySnapshot,
)
from greenkube.gitops.planning import raw_manifest as raw_manifest_module

BASE_REVISION = "a" * 40

DEPLOYMENT = """# source comment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          image: example/api:v1
          resources:
            requests:
              cpu: 100m # retain the inline comment
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
        - name: sidecar
          image: example/sidecar:v1
          resources:
            requests:
              cpu: 50m
"""

GOLDEN_DEPLOYMENT = """# source comment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          image: example/api:v1
          resources:
            requests:
              cpu: 300m # retain the inline comment
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
        - name: sidecar
          image: example/sidecar:v1
          resources:
            requests:
              cpu: 50m
"""


def _source(**overrides) -> WorkloadSource:
    values = {
        "id": "source-1",
        "cluster": "prod",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "api",
        "provider_id": "github-main",
        "repo_url": "https://github.com/acme/gitops.git",
        "ref": "main",
        "revision": BASE_REVISION,
        "path": "apps/payments",
        "controller_kind": ControllerKind.ARGOCD,
        "controller_name": "payments-prod",
        "controller_namespace": "argocd",
        "source_revision": BASE_REVISION,
        "renderer": RendererKind.RAW,
        "renderer_config": {},
        "confidence": 0.98,
        "status": BindingStatus.RESOLVED,
        "evidence": {},
    }
    values.update(overrides)
    return WorkloadSource(**values)


def _action(
    *,
    operations=None,
    target: ResourceTarget | None = None,
    **overrides,
) -> ActionPlanV3:
    values = {
        "action_id": "act-1",
        "recommendation_ids": [42],
        "target": target
        or ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="api",
        ),
        "operations": operations
        or [
            SetContainerResources(
                container="api",
                resources=ContainerResources(
                    requests={"cpu": "300m", "memory": "256Mi"},
                ),
            )
        ],
        "constraints": {},
        "evidence_ref": "recommendation/42",
    }
    values.update(overrides)
    return ActionPlanV3(**values)


def _blob_sha(content: str | bytes) -> str:
    encoded = content.encode("utf-8") if isinstance(content, str) else content
    payload = f"blob {len(encoded)}\0".encode("ascii") + encoded
    return hashlib.sha1(payload).hexdigest()


def _snapshot(files: dict[str, str | bytes]) -> RepositorySnapshot:
    return RepositorySnapshot(
        contents=files,
        original_blob_shas={path: _blob_sha(content) for path, content in files.items()},
    )


def _plan(
    content: str | bytes = DEPLOYMENT,
    *,
    files: dict[str, str | bytes] | None = None,
    action: ActionPlanV3 | None = None,
    source: WorkloadSource | None = None,
):
    snapshot = _snapshot(files or {"apps/payments/deployment.yaml": content})
    return RawManifestPlanner().plan(
        action or _action(),
        source or _source(),
        snapshot,
        BASE_REVISION,
    )


def test_yaml_golden_preserves_comments_targets_only_named_container_and_is_deterministic():
    path = "apps/payments/deployment.yaml"
    snapshot = _snapshot(
        {
            path: DEPLOYMENT,
            "apps/payments/service.yaml": (
                "apiVersion: v1\nkind: Service\nmetadata: {name: api, namespace: payments}\n"
            ),
            "apps/staging/deployment.yaml": DEPLOYMENT.replace("payments", "staging"),
        }
    )

    planner = RawManifestPlanner()
    file_plan = planner.plan(_action(), _source(), snapshot, BASE_REVISION)
    repeated = planner.plan(_action(), _source(), snapshot, BASE_REVISION)

    assert [(edit.path, edit.operation, edit.original_sha) for edit in file_plan.files] == [
        (path, "update", snapshot.original_blob_shas[path])
    ]
    assert file_plan.files[0].content == GOLDEN_DEPLOYMENT
    assert "# retain the inline comment" in file_plan.files[0].content
    assert file_plan.diff.startswith(f"--- a/{path}\n+++ b/{path}\n")
    assert "-              cpu: 100m # retain the inline comment" in file_plan.diff
    assert "+              cpu: 300m # retain the inline comment" in file_plan.diff
    assert "sidecar" not in file_plan.diff
    assert "source comment" not in file_plan.rendered_diff
    assert '"cpu": "100m"' in file_plan.rendered_diff
    assert '"cpu": "300m"' in file_plan.rendered_diff
    assert file_plan.validation.passed
    assert file_plan.validation.target_changed
    assert not file_plan.validation.unrelated_changes
    assert file_plan.validation.requests_within_limits
    assert file_plan.digest == repeated.digest
    assert file_plan.diff == repeated.diff
    assert file_plan.rendered_diff == repeated.rendered_diff


def test_new_base_revision_produces_a_new_plan_identity_for_the_same_action():
    action = _action()
    source = _source()
    snapshot = _snapshot({"apps/payments/deployment.yaml": DEPLOYMENT})
    planner = RawManifestPlanner()

    first = planner.plan(action, source, snapshot, BASE_REVISION)
    second = planner.plan(action, source, snapshot, "b" * 40)
    repeated = planner.plan(action, source, snapshot, BASE_REVISION)

    assert first.plan_id != action.action_id
    assert first.plan_id != second.plan_id
    assert first.plan_id == repeated.plan_id
    assert first.digest == repeated.digest


def test_json_resource_is_patched_and_remains_valid_json():
    path = "apps/payments/deployment.json"
    original = json.dumps(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "payments"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "api",
                                "image": "example/api:v1",
                                "resources": {"limits": {"cpu": "1"}},
                            }
                        ]
                    }
                }
            },
        },
        separators=(",", ":"),
    )

    plan = _plan(files={path: original})
    updated_content = plan.files[0].content
    assert updated_content is not None
    updated = json.loads(updated_content)
    container = updated["spec"]["template"]["spec"]["containers"][0]

    assert plan.files[0].path == path
    assert container["resources"] == {
        "limits": {"cpu": "1"},
        "requests": {"cpu": "300m", "memory": "256Mi"},
    }
    assert plan.validation.passed


def test_yaml_kind_list_patches_only_the_exact_item():
    original = """apiVersion: v1
kind: List
items:
  - apiVersion: v1
    kind: Service
    metadata:
      name: api
      namespace: payments
  - apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: api
      namespace: payments
    spec:
      template:
        spec:
          containers:
            - name: api
              image: example/api:v1
"""

    plan = _plan(original)
    updated_content = plan.files[0].content
    assert updated_content is not None

    assert "kind: List" in updated_content
    assert "requests:" in updated_content
    assert "cpu: 300m" in updated_content
    assert "kind: Service" in updated_content
    assert plan.validation.passed


def test_multi_document_yaml_patches_the_exact_document_and_preserves_others():
    original = """# ConfigMap remains unchanged
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: payments
data:
  mode: stable
---
# The workload document is the only edit target
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          image: example/api:v1
---
apiVersion: v1
kind: Service
metadata:
  name: api
  namespace: payments
"""

    plan = _plan(original)
    updated_content = plan.files[0].content
    assert updated_content is not None

    assert updated_content.count("---") == 2
    assert "# ConfigMap remains unchanged" in updated_content
    assert "# The workload document is the only edit target" in updated_content
    assert "mode: stable" in updated_content
    assert "kind: Service" in updated_content
    assert updated_content.index("kind: ConfigMap") < updated_content.index("kind: Deployment")
    assert updated_content.index("kind: Deployment") < updated_content.index("kind: Service")
    assert plan.validation.passed


def test_multiple_operations_only_change_the_explicit_container_names():
    action = _action(
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            ),
            SetContainerResources(
                container="sidecar",
                resources=ContainerResources(requests={"memory": "64Mi"}),
            ),
        ]
    )

    plan = _plan(action=action)
    updated_content = plan.files[0].content
    assert updated_content is not None

    assert "cpu: 300m" in updated_content
    assert "memory: 64Mi" in updated_content
    assert "memory: 256Mi" not in updated_content
    assert plan.validation.passed


@pytest.mark.parametrize(
    "namespace",
    ["staging"],
)
def test_different_namespace_never_matches_known_binding(namespace):
    original = DEPLOYMENT.replace("namespace: payments", f"namespace: {namespace}")

    with pytest.raises(RawManifestPlanError, match="exact target"):
        _plan(original)


def test_missing_namespace_uses_binding_scope_without_rewriting_source_namespace():
    original = DEPLOYMENT.replace("  namespace: payments\n", "")

    plan = _plan(original)

    assert plan.validation.passed
    assert plan.files[0].content is not None
    assert "namespace: payments" not in plan.files[0].content
    assert "cpu: 300m" in plan.files[0].content


def test_snapshot_is_immutable_and_detached_from_input_mappings():
    path = "apps/payments/deployment.yaml"
    files = {path: DEPLOYMENT}
    shas = {path: _blob_sha(DEPLOYMENT)}
    snapshot = RepositorySnapshot(contents=files, original_blob_shas=shas)
    files[path] = "mutated"
    shas[path] = "mutated"

    assert snapshot.contents[path] == DEPLOYMENT
    assert snapshot.original_blob_shas[path] == _blob_sha(DEPLOYMENT)
    with pytest.raises(TypeError):
        cast(MutableMapping[str, str | bytes], snapshot.contents)["other.yaml"] = "not mutable"


def test_duplicate_target_in_two_source_files_is_ambiguous():
    with pytest.raises(RawManifestPlanError, match="ambiguous|duplicate resource identity"):
        _plan(
            files={
                "apps/payments/one.yaml": DEPLOYMENT,
                "apps/payments/two.yaml": DEPLOYMENT,
            }
        )


def test_rerender_rejects_an_unrelated_resource_change(monkeypatch):
    original_render = raw_manifest_module._render
    render_count = 0

    def render_with_unrelated_change(sources):
        nonlocal render_count
        result = original_render(sources)
        render_count += 1
        if render_count == 2:
            resources = list(result.resources)
            service_index = next(index for index, resource in enumerate(resources) if resource.target.kind == "Service")
            service = resources[service_index]
            changed_manifest = dict(service.manifest)
            changed_manifest["metadata"] = dict(changed_manifest["metadata"])
            changed_manifest["metadata"]["labels"] = {"unexpected": "change"}
            resources[service_index] = replace(service, manifest=changed_manifest)
            return replace(result, resources=tuple(resources))
        return result

    monkeypatch.setattr(raw_manifest_module, "_render", render_with_unrelated_change)
    with pytest.raises(RawManifestPlanError, match="unrelated resource change"):
        _plan(
            files={
                "apps/payments/deployment.yaml": DEPLOYMENT,
                "apps/payments/service.yaml": (
                    "apiVersion: v1\nkind: Service\nmetadata: {name: api, namespace: payments}\n"
                ),
            }
        )


def test_rerender_allows_only_checksum_annotation_value_changes_on_target(monkeypatch):
    original_render = raw_manifest_module._render
    render_count = 0
    content = DEPLOYMENT.replace(
        "spec:\n  template:\n    spec:",
        "spec:\n  template:\n    metadata:\n      annotations:\n        checksum/config: before\n    spec:",
    )

    def render_with_checksum_change(sources):
        nonlocal render_count
        result = original_render(sources)
        render_count += 1
        if render_count == 2:
            resources = list(result.resources)
            deployment_index = next(
                index for index, resource in enumerate(resources) if resource.target.kind == "Deployment"
            )
            deployment = resources[deployment_index]
            changed_manifest = dict(deployment.manifest)
            changed_spec = dict(changed_manifest["spec"])
            changed_template = dict(changed_spec["template"])
            changed_metadata = dict(changed_template["metadata"])
            changed_metadata["annotations"] = {"checksum/config": "after"}
            changed_template["metadata"] = changed_metadata
            changed_spec["template"] = changed_template
            changed_manifest["spec"] = changed_spec
            resources[deployment_index] = replace(deployment, manifest=changed_manifest)
            return replace(result, resources=tuple(resources))
        return result

    monkeypatch.setattr(raw_manifest_module, "_render", render_with_checksum_change)

    file_plan = _plan(content)

    assert file_plan.validation.passed
    assert file_plan.validation.target_changed
    assert not file_plan.validation.unrelated_changes


def test_rerender_rejects_added_checksum_annotation_key(monkeypatch):
    original_render = raw_manifest_module._render
    render_count = 0

    def render_with_new_checksum(sources):
        nonlocal render_count
        result = original_render(sources)
        render_count += 1
        if render_count == 2:
            resources = list(result.resources)
            deployment_index = next(
                index for index, resource in enumerate(resources) if resource.target.kind == "Deployment"
            )
            deployment = resources[deployment_index]
            changed_manifest = dict(deployment.manifest)
            changed_spec = dict(changed_manifest["spec"])
            changed_template = dict(changed_spec["template"])
            changed_template["metadata"] = {"annotations": {"checksum/config": "unexpected"}}
            changed_spec["template"] = changed_template
            changed_manifest["spec"] = changed_spec
            resources[deployment_index] = replace(deployment, manifest=changed_manifest)
            return replace(result, resources=tuple(resources))
        return result

    monkeypatch.setattr(raw_manifest_module, "_render", render_with_new_checksum)
    with pytest.raises(RawManifestPlanError, match="changes outside the requested container resources"):
        _plan()


def test_source_outside_binding_scope_is_not_selected():
    with pytest.raises(RawManifestPlanError, match="exact target"):
        _plan(files={"apps/other/deployment.yaml": DEPLOYMENT})


def test_action_target_must_match_binding_identity():
    target = ResourceTarget(
        api_version="apps/v1",
        kind="Deployment",
        namespace="staging",
        name="api",
    )

    with pytest.raises(RawManifestPlanError, match="binding"):
        _plan(action=_action(target=target))


@pytest.mark.parametrize(
    "operation",
    [
        DeleteResource(),
        AddHorizontalPodAutoscaler(min_replicas=1, max_replicas=2),
    ],
)
def test_non_rightsizing_action_is_rejected(operation):
    with pytest.raises(RawManifestPlanError, match="per-container rightsizing"):
        _plan(action=_action(operations=[operation]))


def test_empty_resource_operation_is_rejected():
    action = _action(operations=[SetContainerResources(container="api", resources=ContainerResources())])

    with pytest.raises(RawManifestPlanError, match="at least one resource"):
        _plan(action=action)


def test_requests_must_not_exceed_matching_limits():
    too_large = DEPLOYMENT.replace("cpu: 300m", "cpu: 600m").replace("cpu: 100m", "cpu: 600m")
    action = _action(
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "600m"}),
            )
        ]
    )

    with pytest.raises(RawManifestPlanError, match="request.*exceeds.*limit"):
        _plan(too_large, action=action)


def test_invalid_existing_resource_quantity_is_rejected_even_if_not_modified():
    original = DEPLOYMENT.replace("memory: 128Mi", "memory: 128Mii")
    action = _action(
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ]
    )

    with pytest.raises(RawManifestPlanError, match="quantity"):
        _plan(original, action=action)


def test_missing_container_is_rejected():
    action = _action(
        operations=[
            SetContainerResources(
                container="missing",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ]
    )

    with pytest.raises(RawManifestPlanError, match="container.*exactly once"):
        _plan(action=action)


def test_duplicate_container_name_is_rejected_as_ambiguous():
    original = DEPLOYMENT.replace(
        "        - name: sidecar\n",
        "        - name: api\n",
    )

    with pytest.raises(RawManifestPlanError, match="container.*exactly once"):
        _plan(original)


def test_action_cannot_cross_renderer_or_unresolved_binding():
    with pytest.raises(RawManifestPlanError, match="raw renderer"):
        _plan(source=_source(renderer=RendererKind.KUSTOMIZE))

    with pytest.raises(RawManifestPlanError, match="resolved"):
        _plan(source=_source(status=BindingStatus.AMBIGUOUS))


def test_unsupported_workload_kind_is_rejected():
    target = ResourceTarget(
        api_version="example.com/v1",
        kind="CustomWorkload",
        namespace="payments",
        name="api",
    )
    source = _source(workload_kind="CustomWorkload")
    action = _action(target=target)
    manifest = DEPLOYMENT.replace("apps/v1", "example.com/v1").replace("Deployment", "CustomWorkload")

    with pytest.raises(RawManifestPlanError, match="unsupported workload kind"):
        _plan(manifest, action=action, source=source)


def test_malformed_templated_source_and_missing_blob_sha_are_rejected():
    templated = "apiVersion: apps/v1\nkind: {{ .Values.kind }}\n"
    with pytest.raises(RawManifestPlanError, match="render|YAML"):
        _plan(templated)

    path = "apps/payments/deployment.yaml"
    incomplete_snapshot = RepositorySnapshot(contents={path: DEPLOYMENT}, original_blob_shas={})
    with pytest.raises(RawManifestPlanError, match="blob SHA"):
        RawManifestPlanner().plan(_action(), _source(), incomplete_snapshot, BASE_REVISION)


def test_kubernetes_quantity_comparison_uses_numeric_values_not_string_order():
    original = DEPLOYMENT.replace("cpu: 500m", 'cpu: "1"')
    action = _action(
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "1000m"}),
            )
        ]
    )

    plan = _plan(original, action=action)

    assert plan.validation.requests_within_limits


def test_binary_kubernetes_quantities_are_compared_exactly():
    original = DEPLOYMENT.replace("memory: 512Mi", "memory: 2Ei")
    action = _action(
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"memory": "1Ei"}),
            )
        ]
    )

    plan = _plan(original, action=action)

    assert plan.validation.requests_within_limits


def test_out_of_range_kubernetes_quantity_is_rejected():
    action = _action(
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "10E"}),
            )
        ]
    )

    with pytest.raises(RawManifestPlanError, match="outside the supported range"):
        _plan(action=action)


def test_duplicate_container_operations_are_rejected_by_action_contract():
    operations = [
        SetContainerResources(container="api", resources=ContainerResources(requests={"cpu": "300m"})),
        SetContainerResources(container="api", resources=ContainerResources(requests={"memory": "256Mi"})),
    ]

    with pytest.raises(ValidationError, match="one resource operation"):
        _action(operations=operations)
