# tests/gitops/planning/test_plan_digest.py
"""Every planner signs its plan with the shared digest (roadmap M6)."""

import pytest

from greenkube.gitops.contracts import FileEdit, RendererKind, ValidationResult
from greenkube.gitops.planning.digest import plan_digest
from greenkube.gitops.planning.raw_manifest import RawManifestPlanError

from .test_raw_manifest import _action, _source  # pyrefly: ignore[missing-import]


def _digest(files, **overrides):
    arguments = {
        "plan_id": "pln-1",
        "action": _action(),
        "source": _source(),
        "files": files,
        "diff": "--- a\n+++ b\n",
        "rendered_diff": "",
        "base_revision": "a" * 40,
        "source_revision": None,
        "validation": ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest="sha256:" + "d" * 64,
        ),
        "warnings": [],
        "renderer": RendererKind.RAW,
        "error_type": RawManifestPlanError,
    }
    arguments.update(overrides)
    return plan_digest(**arguments)


def _edit(path: str) -> FileEdit:
    return FileEdit(path=path, operation="update", original_sha="b" * 40, content="kind: Deployment\n")


def test_digest_is_deterministic_and_sensitive_to_every_signed_input():
    files = [_edit("apps/a.yaml")]
    digest = _digest(files)

    assert digest == _digest(files)
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert digest != _digest(files, renderer=RendererKind.KUSTOMIZE)
    assert digest != _digest(files, warnings=["w"])
    assert digest != _digest([_edit("apps/b.yaml")])


def test_files_are_signed_in_the_order_the_planner_gives():
    first, second = _edit("apps/a.yaml"), _edit("apps/b.yaml")

    assert _digest([first, second]) != _digest([second, first])


def test_non_serializable_inputs_raise_the_planner_error():
    with pytest.raises(RawManifestPlanError, match="deterministically JSON-serializable"):
        _digest([_edit("apps/a.yaml")], warnings=[float("nan")])
