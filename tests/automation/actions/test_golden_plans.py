"""Golden file plans: the action framework must keep every rendered diff byte-identical.

The golden file was generated with the planners as they were before the action
framework (H0) and must not change when planners move behind ``PatchStrategy``.
Regenerate it only for an intentional output change:
``GREENKUBE_UPDATE_GOLDEN=1 uv run pytest tests/automation/actions/test_golden_plans.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from greenkube.automation.rollback import KustomizeRollbackRenderer, build_inverse_plan
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ContainerResources,
    ControllerKind,
    FilePlan,
    Plan,
    PlanStatus,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.planning.helm import FluxHelmReleasePlanner, HelmValuesPlanner
from greenkube.gitops.planning.kustomize import KustomizePlanner
from greenkube.gitops.planning.raw_manifest import RawManifestPlanner, RepositorySnapshot
from greenkube.gitops.renderers.kustomize import KustomizeRenderer
from tests.gitops.planning import test_helm as helm_cases  # pyrefly: ignore[missing-import]
from tests.gitops.planning import test_kustomize as kustomize_cases  # pyrefly: ignore[missing-import]
from tests.gitops.planning import test_kustomize_flux as flux_cases  # pyrefly: ignore[missing-import]
from tests.gitops.planning import test_raw_manifest as raw_cases  # pyrefly: ignore[missing-import]

GOLDEN_PATH = Path(__file__).with_name("golden_plans.json")
BASE_REVISION = "a" * 40
CURRENT_BASE = "b" * 40
SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: api\n  namespace: payments\n"


def _git_blob_sha(content: str) -> str:
    import hashlib

    payload = content.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


def _raw_plan(files: Mapping[str, str], action: ActionPlanV3, source: WorkloadSource | None = None) -> FilePlan:
    snapshot = RepositorySnapshot(
        contents=dict(files),
        original_blob_shas={path: _git_blob_sha(content) for path, content in files.items()},
    )
    return RawManifestPlanner().plan(action, source or raw_cases._source(), snapshot, BASE_REVISION)


def _ops(**containers: dict[str, dict[str, str]]) -> list[SetContainerResources]:
    return [
        SetContainerResources(container=name.replace("_", "-"), resources=ContainerResources(**resources))
        for name, resources in containers.items()
    ]


def _raw_cases() -> dict[str, Callable[[Path], Any]]:
    deployment = raw_cases.DEPLOYMENT
    namespace_less = deployment.replace("  namespace: payments\n", "", 1)
    json_manifest = json.dumps(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "payments"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "api", "image": "example/api:v1", "resources": {"requests": {"cpu": "100m"}}}
                        ]
                    }
                }
            },
        },
        indent=2,
    )
    cronjob = """apiVersion: batch/v1
kind: CronJob
metadata:
  name: api
  namespace: payments
spec:
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: api
              image: example/api:v1
"""
    list_manifest = """apiVersion: v1
kind: List
items:
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
              resources:
                requests:
                  cpu: 100m
  - apiVersion: v1
    kind: Service
    metadata:
      name: api
      namespace: payments
"""
    multi = raw_cases._action(
        operations=_ops(
            api={"requests": {"cpu": "300m", "memory": "256Mi"}},
            sidecar={"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"memory": "64Mi"}},
        )
    )
    cronjob_target = ResourceTarget(api_version="batch/v1", kind="CronJob", namespace="payments", name="api")
    return {
        "raw_yaml_single": lambda _: _raw_plan({"apps/payments/deployment.yaml": deployment}, raw_cases._action()),
        "raw_yaml_multi_container_with_limits": lambda _: _raw_plan(
            {
                "apps/payments/deployment.yaml": deployment,
                "apps/payments/service.yaml": SERVICE,
            },
            multi,
        ),
        "raw_yaml_namespace_less": lambda _: _raw_plan(
            {"apps/payments/deployment.yaml": namespace_less}, raw_cases._action()
        ),
        "raw_json": lambda _: _raw_plan({"apps/payments/deployment.json": json_manifest + "\n"}, raw_cases._action()),
        "raw_list": lambda _: _raw_plan({"apps/payments/list.yaml": list_manifest}, raw_cases._action()),
        "raw_cronjob": lambda _: _raw_plan(
            {"apps/payments/cronjob.yaml": cronjob},
            raw_cases._action(target=cronjob_target),
            raw_cases._source(workload_kind="CronJob"),
        ),
    }


def _kustomize_plan(
    tmp_path: Path,
    files: Mapping[str, str],
    action: ActionPlanV3,
    source: WorkloadSource,
    runner: Any,
) -> FilePlan:
    snapshot = kustomize_cases._snapshot(tmp_path, files)
    try:
        return KustomizePlanner(runner=runner).plan(action, source, snapshot, BASE_REVISION)
    finally:
        snapshot.close()


def _apply(files: Mapping[str, str], plan: FilePlan) -> dict[str, str]:
    updated = dict(files)
    for edit in plan.files:
        assert edit.content is not None
        updated[edit.path] = edit.content
    return updated


def _kustomize_existing_patch(tmp_path: Path) -> FilePlan:
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    files = kustomize_cases.BASE_FILES
    first = _kustomize_plan(
        first_dir, files, kustomize_cases._action(), kustomize_cases._source(), kustomize_cases._FakeKustomizeRunner()
    )
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    action = kustomize_cases._action().model_copy(
        update={"operations": _ops(sidecar={"requests": {"cpu": "20m"}}), "action_id": "act-2"}
    )
    return _kustomize_plan(
        second_dir, _apply(files, first), action, kustomize_cases._source(), kustomize_cases._FakeKustomizeRunner()
    )


def _kustomize_cases() -> dict[str, Callable[[Path], Any]]:
    argo_namespace_less = flux_cases._flux_source(target_namespace=None).model_copy(
        update={"controller_kind": ControllerKind.ARGOCD, "controller_namespace": "argocd"}
    )
    multi = kustomize_cases._action().model_copy(
        update={
            "operations": _ops(
                api={"requests": {"cpu": "300m", "memory": "256Mi"}}, sidecar={"requests": {"memory": "32Mi"}}
            )
        }
    )
    return {
        "kustomize_argo": lambda tmp: _kustomize_plan(
            tmp,
            kustomize_cases.BASE_FILES,
            kustomize_cases._action(),
            kustomize_cases._source(),
            kustomize_cases._FakeKustomizeRunner(),
        ),
        "kustomize_argo_multi_container": lambda tmp: _kustomize_plan(
            tmp, kustomize_cases.BASE_FILES, multi, kustomize_cases._source(), kustomize_cases._FakeKustomizeRunner()
        ),
        "kustomize_existing_managed_patch": _kustomize_existing_patch,
        "kustomize_flux_target_namespace": lambda tmp: _kustomize_plan(
            tmp,
            flux_cases._layout(),
            flux_cases._action(),
            flux_cases._flux_source(),
            flux_cases._KustomizeSemanticsRunner(),
        ),
        "kustomize_flux_name_prefix": lambda tmp: _kustomize_plan(
            tmp,
            flux_cases._layout(),
            flux_cases._action(name="eu-worker"),
            flux_cases._flux_source(name="eu-worker", name_prefix="eu-"),
            flux_cases._KustomizeSemanticsRunner(),
        ),
        "kustomize_argo_namespace_less": lambda tmp: _kustomize_plan(
            tmp, flux_cases._layout(), flux_cases._action(), argo_namespace_less, flux_cases._KustomizeSemanticsRunner()
        ),
    }


def _helm_values(_: Path) -> FilePlan:
    files = {
        **helm_cases._CHART,
        "apps/prod/values.yaml": "# comment\napp:\n  resources:\n    requests:\n      cpu: 100m\n",
    }
    snapshot = helm_cases._snapshot(files)
    try:
        planner = HelmValuesPlanner(renderer=helm_cases._helm_renderer(helm_cases._ChartRunner()))
        return planner.plan(
            helm_cases._action(),
            helm_cases._values_source(),
            snapshot,
            BASE_REVISION,
            mapping=helm_cases._values_mapping(),
        )
    finally:
        snapshot.close()


def _flux_helm_release(_: Path) -> FilePlan:
    files = {**helm_cases._CHART, "clusters/prod/helmrelease.yaml": helm_cases._release_manifest()}
    snapshot = helm_cases._snapshot(files)
    try:
        planner = FluxHelmReleasePlanner(renderer=helm_cases._helm_renderer(helm_cases._ChartRunner()))
        return planner.plan(helm_cases._action(), helm_cases._flux_source(), snapshot, BASE_REVISION)
    finally:
        snapshot.close()


def _persisted(file_plan: FilePlan, action: ActionPlanV3, source: WorkloadSource) -> Plan:
    return Plan(
        plan_id=file_plan.plan_id,
        recommendation_ids=action.recommendation_ids,
        source=source,
        action=action,
        file_plan=file_plan,
        status=PlanStatus.APPLIED,
        digest=file_plan.digest,
    )


def _raw_rollback(_: Path) -> dict[str, Any]:
    files = {"apps/payments/deployment.yaml": raw_cases.DEPLOYMENT}
    action = raw_cases._action()
    forward = _raw_plan(files, action)
    inverse = build_inverse_plan(
        _persisted(forward, action, raw_cases._source()), files, _apply(files, forward), CURRENT_BASE
    )
    return _inverse_dump(inverse)


def _kustomize_rollback_case(tmp_path: Path) -> dict[str, Any]:
    files = kustomize_cases.BASE_FILES
    action = kustomize_cases._action()
    source = kustomize_cases._source()
    forward = _kustomize_plan(tmp_path, files, action, source, kustomize_cases._FakeKustomizeRunner())
    originals = {edit.path: files[edit.path] for edit in forward.files if edit.operation == "update"}
    renderer = KustomizeRollbackRenderer(
        KustomizeRenderer(expected_version="v5.8.3", runner=kustomize_cases._FakeKustomizeRunner())
    )
    inverse = build_inverse_plan(
        _persisted(forward, action, source), originals, _apply(files, forward), CURRENT_BASE, renderer=renderer
    )
    return _inverse_dump(inverse)


def _inverse_dump(inverse: Any) -> dict[str, Any]:
    return {
        "plan_id": inverse.plan_id,
        "files": [edit.model_dump(mode="json") for edit in inverse.files],
        "diff": inverse.diff,
        "rendered_diff": inverse.rendered_diff,
        "rendered_before_digest": inverse.rendered_before_digest,
        "rendered_after_digest": inverse.rendered_after_digest,
        "digest": inverse.digest,
    }


def _cases() -> dict[str, Callable[[Path], Any]]:
    return {
        **_raw_cases(),
        **_kustomize_cases(),
        "helm_values_same_repo": _helm_values,
        "flux_helm_release": _flux_helm_release,
        "rollback_raw": _raw_rollback,
        "rollback_kustomize": _kustomize_rollback_case,
    }


def _dump(result: Any) -> Any:
    return result.model_dump(mode="json") if isinstance(result, FilePlan) else result


@pytest.mark.parametrize("name", sorted(_cases()))
def test_rendered_plans_are_byte_identical_to_the_golden_outputs(name: str, tmp_path: Path) -> None:
    actual = _dump(_cases()[name](tmp_path))
    golden: dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8")) if GOLDEN_PATH.exists() else {}
    if os.environ.get("GREENKUBE_UPDATE_GOLDEN") == "1":
        golden[name] = actual
        GOLDEN_PATH.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert name in golden, f"no golden output for {name}; regenerate with GREENKUBE_UPDATE_GOLDEN=1"
    assert actual == golden[name]
