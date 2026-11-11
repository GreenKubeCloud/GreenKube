"""Rollback accepts the namespace-less target match that forward planning accepts (D14 follow-up).

Argo CD supplies its destination namespace to namespace-less manifests, so forward
planning matches a rendered ``Deployment`` without ``metadata.namespace`` against the
live, namespaced target. The inverse must validate against the same rendered identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from greenkube.automation.rollback import (
    KustomizeRollbackRenderer,
    RollbackValidationError,
    build_inverse_plan,
)
from greenkube.gitops.contracts import ControllerKind, FilePlan, Plan, PlanStatus
from greenkube.gitops.planning.kustomize import KustomizePlanner
from greenkube.gitops.planning.raw_manifest import RawManifestPlanner, RepositorySnapshot
from greenkube.gitops.renderers.kustomize import KustomizeRenderer
from tests.gitops.planning import test_kustomize_flux as flux_cases  # pyrefly: ignore[missing-import]
from tests.gitops.planning import test_raw_manifest as raw_cases  # pyrefly: ignore[missing-import]

BASE_REVISION = "a" * 40
CURRENT_BASE = "b" * 40


def _blob_sha(content: str) -> str:
    payload = content.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


def _apply(files: Mapping[str, str], plan: FilePlan) -> dict[str, str]:
    updated = dict(files)
    for edit in plan.files:
        assert edit.content is not None
        updated[edit.path] = edit.content
    return updated


def _persisted(file_plan: FilePlan, action, source) -> Plan:
    return Plan(
        plan_id=file_plan.plan_id,
        recommendation_ids=action.recommendation_ids,
        source=source,
        action=action,
        file_plan=file_plan,
        status=PlanStatus.APPLIED,
        digest=file_plan.digest,
    )


def _argo_source():
    return flux_cases._flux_source(target_namespace=None).model_copy(
        update={"controller_kind": ControllerKind.ARGOCD, "controller_namespace": "argocd"}
    )


def test_argocd_kustomize_rollback_accepts_the_namespace_less_match_of_forward_planning(tmp_path: Path) -> None:
    files = flux_cases._layout()
    action = flux_cases._action()
    source = _argo_source()
    snapshot = flux_cases._snapshot(tmp_path, files)
    try:
        forward = KustomizePlanner(runner=flux_cases._KustomizeSemanticsRunner()).plan(
            action, source, snapshot, BASE_REVISION
        )
    finally:
        snapshot.close()
    originals = {edit.path: files[edit.path] for edit in forward.files if edit.operation == "update"}
    renderer = KustomizeRollbackRenderer(
        KustomizeRenderer(expected_version="v5.8.3", runner=flux_cases._KustomizeSemanticsRunner())
    )

    inverse = build_inverse_plan(
        _persisted(forward, action, source), originals, _apply(files, forward), CURRENT_BASE, renderer=renderer
    )

    assert sorted((edit.path, edit.operation) for edit in inverse.files) == sorted(
        (edit.path, "delete" if edit.operation == "create" else "update") for edit in forward.files
    )
    assert '"cpu": "50m"' in inverse.rendered_diff
    assert '"cpu": "300m"' in inverse.rendered_diff


def test_raw_rollback_accepts_a_namespace_less_manifest_deployed_by_argocd() -> None:
    path = "apps/payments/deployment.yaml"
    files = {path: raw_cases.DEPLOYMENT.replace("  namespace: payments\n", "", 1)}
    action = raw_cases._action()
    source = raw_cases._source()
    forward = RawManifestPlanner().plan(
        action,
        source,
        RepositorySnapshot(contents=files, original_blob_shas={path: _blob_sha(files[path])}),
        BASE_REVISION,
    )

    inverse = build_inverse_plan(_persisted(forward, action, source), files, _apply(files, forward), CURRENT_BASE)

    assert [(edit.path, edit.operation, edit.content) for edit in inverse.files] == [(path, "update", files[path])]


def test_flux_kustomize_rollback_still_requires_the_exact_rendered_identity(tmp_path: Path) -> None:
    """Flux namespaces resources only through targetNamespace, so a namespace-less render never matches."""
    files = flux_cases._layout()
    action = flux_cases._action()
    flux_source = flux_cases._flux_source()
    snapshot = flux_cases._snapshot(tmp_path, files)
    try:
        forward = KustomizePlanner(runner=flux_cases._KustomizeSemanticsRunner()).plan(
            action, flux_source, snapshot, BASE_REVISION
        )
    finally:
        snapshot.close()
    originals = {edit.path: files[edit.path] for edit in forward.files if edit.operation == "update"}
    # The binding lost its targetNamespace: the render is namespace-less and must not match.
    stale_source = flux_source.model_copy(
        update={"renderer_config": {k: v for k, v in flux_source.renderer_config.items() if k != "target_namespace"}}
    )
    plan = _persisted(forward, action, flux_source).model_copy(update={"source": stale_source})
    renderer = KustomizeRollbackRenderer(
        KustomizeRenderer(expected_version="v5.8.3", runner=flux_cases._KustomizeSemanticsRunner())
    )

    with pytest.raises(RollbackValidationError, match="exact resource target"):
        build_inverse_plan(plan, originals, _apply(files, forward), CURRENT_BASE, renderer=renderer)
