"""Render inputs recorded with a plan (delivery safety, D7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greenkube.gitops.contracts import FileEdit, RendererKind, ValidationResult
from greenkube.gitops.planning.render_inputs import (
    changed_render_inputs,
    git_blob_sha,
    inverse_render_inputs,
    records_render_inputs,
)

GOLDEN = Path(__file__).parents[2] / "automation" / "actions" / "golden_plans.json"


def _validation(**updates) -> ValidationResult:
    return ValidationResult(
        target_changed=True,
        unrelated_changes=False,
        requests_within_limits=True,
        renderer=RendererKind.KUSTOMIZE,
        output_digest="sha256:" + "a" * 64,
        **updates,
    )


def test_git_blob_sha_matches_git_hash_object():
    # printf 'hello\n' | git hash-object --stdin
    assert git_blob_sha("hello\n", "a" * 40) == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_an_empty_record_keeps_the_legacy_json_form_and_digest():
    assert "render_inputs" not in _validation().model_dump(mode="json")
    assert "render_scopes" not in _validation().model_dump(mode="json")
    assert not records_render_inputs(_validation())


def test_render_inputs_are_validated():
    with pytest.raises(ValueError):
        _validation(render_inputs={"../escape.yaml": "a" * 40})
    with pytest.raises(ValueError):
        _validation(render_inputs={"base/deployment.yaml": "not-a-sha"})


def test_changed_inputs_and_scopes_are_reported():
    validation = _validation(
        render_inputs={"apps/base/deployment.yaml": "a" * 40, "apps/prod/kustomization.yaml": "b" * 40},
        render_scopes=["charts/payments"],
    )

    assert changed_render_inputs(validation, ["apps/other/x.yaml"]) == []
    assert changed_render_inputs(
        validation, ["apps/base/deployment.yaml", "charts/payments/templates/new.yaml", "apps/other/x.yaml"]
    ) == ["apps/base/deployment.yaml", "charts/payments/templates/new.yaml"]


def test_rollback_inputs_watch_every_original_input():
    original = _validation(render_inputs={"apps/base/deployment.yaml": "a" * 40}, render_scopes=["apps/prod"])
    edits = [FileEdit(path="apps/prod/patch.yaml", operation="update", original_sha="c" * 40, content="x")]

    derived = inverse_render_inputs(original, edits)

    assert derived["render_inputs"] == {"apps/prod/patch.yaml": "c" * 40}
    assert derived["render_scopes"] == ["apps/base/deployment.yaml", "apps/prod"]
    assert inverse_render_inputs(_validation(), edits) == {}


def test_planners_record_bases_patches_values_and_chart_files():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    kustomize = golden["kustomize_argo"]["validation"]["render_inputs"]
    assert {"apps/payments/base/deployment.yaml", "apps/payments/base/kustomization.yaml"} <= set(kustomize)
    helm = golden["helm_values_same_repo"]["validation"]
    assert {"charts/payments/Chart.yaml", "charts/payments/values-prod.yaml"} <= set(helm["render_inputs"])
    assert helm["render_scopes"] == ["charts/payments"]
    raw = golden["raw_yaml_multi_container_with_limits"]["validation"]
    assert set(raw["render_inputs"]) == {"apps/payments/deployment.yaml", "apps/payments/service.yaml"}
    assert raw["render_scopes"] == ["apps/payments"]
