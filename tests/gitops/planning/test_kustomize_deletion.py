"""Kustomize deletion plans (roadmap H6): remove a ``resources:`` entry or one document."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenkube.gitops.contracts import ActionPlanV3, DeleteResource, ResourceTarget
from greenkube.gitops.discovery.inventory import CONTROLLER_PRUNE_EVIDENCE
from greenkube.gitops.planning.kustomize import KustomizePlanError, KustomizePlanner
from tests.gitops.planning.test_kustomize import (  # pyrefly: ignore[missing-import]
    BASE_REVISION,
    DEPLOYMENT,
    OVERLAY_PATH,
    _FakeKustomizeRunner,
    _snapshot,
    _source,
)

LB = """\
apiVersion: v1
kind: Service
metadata:
  name: old-lb
  namespace: payments
spec:
  type: LoadBalancer
  selector:
    app: old
"""
TARGET = ResourceTarget(api_version="v1", kind="Service", namespace="payments", name="old-lb")


def _action() -> ActionPlanV3:
    return ActionPlanV3(
        action_id="act-delete",
        recommendation_ids=[7],
        target=TARGET,
        operations=[DeleteResource()],
        constraints={"resource_deletion": True},
        evidence_ref="recommendation-evidence/7",
    )


def _resource_source():
    return _source(
        workload_kind="Service",
        workload_name="old-lb",
        evidence={CONTROLLER_PRUNE_EVIDENCE: True, "resource_binding": True},
    )


def _files(**overrides: str) -> dict[str, str]:
    files = {
        f"{OVERLAY_PATH}/kustomization.yaml": "resources:\n  - ../../base\n",
        "apps/payments/base/kustomization.yaml": (
            "# base resources\nresources:\n  - deployment.yaml\n  - lb.yaml # orphaned\n"
        ),
        "apps/payments/base/deployment.yaml": DEPLOYMENT,
        "apps/payments/base/lb.yaml": LB,
    }
    files.update(overrides)
    return files


def test_a_single_resource_file_and_its_resources_entry_are_removed(tmp_path: Path) -> None:
    with _snapshot(tmp_path, _files()) as snapshot:
        plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
            _action(), _resource_source(), snapshot, BASE_REVISION
        )

    edits = {edit.path: edit for edit in plan.files}
    assert edits["apps/payments/base/lb.yaml"].operation == "delete"
    kustomization = edits["apps/payments/base/kustomization.yaml"]
    assert kustomization.operation == "update"
    assert kustomization.content == "# base resources\nresources:\n  - deployment.yaml\n"
    assert plan.validation.passed
    assert "+++ /dev/null" in plan.diff


def test_one_document_of_a_multi_document_file_is_removed(tmp_path: Path) -> None:
    files = _files(
        **{
            "apps/payments/base/kustomization.yaml": "resources:\n  - all.yaml\n",
            "apps/payments/base/all.yaml": DEPLOYMENT + "---\n" + LB,
        }
    )
    del files["apps/payments/base/deployment.yaml"]
    del files["apps/payments/base/lb.yaml"]
    with _snapshot(tmp_path, files) as snapshot:
        plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
            _action(), _resource_source(), snapshot, BASE_REVISION
        )

    (edit,) = plan.files
    assert edit.path == "apps/payments/base/all.yaml" and edit.operation == "update"
    assert edit.content is not None and "old-lb" not in edit.content and "kind: Deployment" in edit.content


def test_a_file_listed_by_another_kustomization_is_not_deleted(tmp_path: Path) -> None:
    files = _files(
        **{
            "apps/other/kustomization.yaml": "resources:\n  - ../payments/base/lb.yaml\n",
        }
    )
    with _snapshot(tmp_path, files) as snapshot:
        with pytest.raises(KustomizePlanError, match="several Kustomizations"):
            KustomizePlanner(runner=_FakeKustomizeRunner()).plan(_action(), _resource_source(), snapshot, BASE_REVISION)


def test_an_opted_out_service_is_never_deleted(tmp_path: Path) -> None:
    opted_out = LB.replace(
        "  namespace: payments\n", "  namespace: payments\n  annotations:\n    greenkube.io/keep: yes\n"
    )
    with _snapshot(tmp_path, _files(**{"apps/payments/base/lb.yaml": opted_out})) as snapshot:
        with pytest.raises(KustomizePlanError, match="opted out"):
            KustomizePlanner(runner=_FakeKustomizeRunner()).plan(_action(), _resource_source(), snapshot, BASE_REVISION)
