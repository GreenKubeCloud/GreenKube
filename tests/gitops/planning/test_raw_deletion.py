"""Raw-manifest deletion plans (roadmap H6): delete the file, one document, or one List item."""

from __future__ import annotations

import json

import pytest

from greenkube.automation.actions import DELETE_RESOURCE
from greenkube.gitops.contracts import ActionPlanV3, DeleteResource, ResourceTarget
from greenkube.gitops.discovery.inventory import CLUSTER_SCOPE_NAMESPACE, CONTROLLER_PRUNE_EVIDENCE
from greenkube.gitops.planning import RawManifestPlanError, RawManifestPlanner
from greenkube.gitops.renderers import render_raw
from tests.gitops.planning.test_raw_manifest import (  # pyrefly: ignore[missing-import]
    BASE_REVISION,
    DEPLOYMENT,
    _snapshot,
    _source,
)

SERVICE_TARGET = ResourceTarget(api_version="v1", kind="Service", namespace="payments", name="old-lb")
PV_TARGET = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-orphan")

LB = """apiVersion: v1
kind: Service
metadata:
  name: old-lb
  namespace: payments
spec:
  type: LoadBalancer
  selector:
    app: old
  ports:
    - port: 80
"""
PV = """apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-orphan
spec:
  capacity:
    storage: 10Gi
  persistentVolumeReclaimPolicy: Retain
  accessModes: [ReadWriteOnce]
"""


def _delete_action(target: ResourceTarget) -> ActionPlanV3:
    return ActionPlanV3(
        action_id="act-delete",
        recommendation_ids=[7],
        target=target,
        operations=[DeleteResource()],
        constraints={"resource_deletion": True},
        evidence_ref="recommendation-evidence/7",
    )


def _resource_source(target: ResourceTarget, *, prune: bool | None = True):
    return _source(
        id="argocd-resource",
        namespace=target.namespace or CLUSTER_SCOPE_NAMESPACE,
        workload_kind=target.kind,
        workload_name=target.name,
        evidence={CONTROLLER_PRUNE_EVIDENCE: prune, "resource_binding": True},
    )


def _plan(files: dict[str, str], target: ResourceTarget = SERVICE_TARGET, *, prune: bool | None = True):
    return RawManifestPlanner().plan(
        _delete_action(target), _resource_source(target, prune=prune), _snapshot(files), BASE_REVISION
    )


def test_a_file_holding_only_the_resource_is_deleted() -> None:
    plan = _plan({"apps/payments/deployment.yaml": DEPLOYMENT, "apps/payments/lb.yaml": LB})

    (edit,) = plan.files
    assert (edit.path, edit.operation, edit.content) == ("apps/payments/lb.yaml", "delete", None)
    assert edit.original_sha is not None
    assert "+++ /dev/null" in plan.diff
    assert plan.validation.passed
    removed = [line for line in plan.rendered_diff.splitlines() if line.startswith("-") and "old-lb" in line]
    assert removed and not any(line.startswith("+") and "old-lb" in line for line in plan.rendered_diff.splitlines())
    assert not any(warning.startswith("deletion_prune_disabled") for warning in plan.warnings)


def test_one_document_of_a_multi_document_file_is_removed() -> None:
    content = DEPLOYMENT + "---\n" + LB
    plan = _plan({"apps/payments/all.yaml": content})

    (edit,) = plan.files
    assert edit.operation == "update"
    assert edit.content is not None
    rendered = render_raw({"apps/payments/all.yaml": edit.content})
    assert [(item.target.kind, item.target.name) for item in rendered.resources] == [("Deployment", "api")]
    assert "# source comment" in edit.content


def test_one_item_of_a_list_is_removed() -> None:
    document = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "keep", "namespace": "payments"}},
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "old-lb", "namespace": "payments"},
                "spec": {"type": "LoadBalancer", "selector": {"app": "old"}},
            },
        ],
    }
    plan = _plan({"apps/payments/list.json": json.dumps(document, indent=2) + "\n"})

    (edit,) = plan.files
    assert edit.operation == "update" and edit.content is not None
    assert [item["metadata"]["name"] for item in json.loads(edit.content)["items"]] == ["keep"]


def test_a_cluster_scoped_volume_is_deleted_and_the_retained_disk_is_explained() -> None:
    plan = _plan({"apps/payments/pv.yaml": PV}, PV_TARGET, prune=False)

    assert plan.files[0].operation == "delete"
    assert any(warning.startswith("persistent_volume_retained") for warning in plan.warnings)
    assert any(warning.startswith("deletion_prune_disabled") for warning in plan.validation.warnings)


def test_an_opted_out_resource_is_never_deleted() -> None:
    opted_out = LB.replace(
        "  namespace: payments\n", '  namespace: payments\n  annotations:\n    greenkube.io/ignore: "true"\n'
    )
    with pytest.raises(RawManifestPlanError, match="opted out"):
        _plan({"apps/payments/lb.yaml": opted_out})


def test_a_service_that_is_not_a_load_balancer_in_git_is_refused() -> None:
    with pytest.raises(RawManifestPlanError, match="not a LoadBalancer"):
        _plan({"apps/payments/lb.yaml": LB.replace("type: LoadBalancer", "type: ClusterIP")})


def test_infrastructure_namespaces_are_refused() -> None:
    target = ResourceTarget(api_version="v1", kind="Service", namespace="kube-system", name="old-lb")
    with pytest.raises(RawManifestPlanError, match="infrastructure namespace"):
        _plan({"apps/payments/lb.yaml": LB.replace("payments", "kube-system")}, target)


def test_a_missing_resource_is_not_guessed() -> None:
    with pytest.raises(RawManifestPlanError, match="no exact target resource"):
        _plan({"apps/payments/deployment.yaml": DEPLOYMENT})


def test_the_kind_compiles_to_one_delete_primitive_of_the_target() -> None:
    (primitive,) = DELETE_RESOURCE.compile(SERVICE_TARGET, [DeleteResource()])
    assert primitive.op == "delete" and primitive.ref == SERVICE_TARGET
