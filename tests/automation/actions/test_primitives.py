"""Primitive operations: paths, list selectors and field edits (H0b)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from greenkube.automation.actions.primitives import (
    CreateResource,
    ListSelector,
    PrimitiveApplyError,
    RemoveField,
    SetField,
    apply_field_ops,
    format_path,
    parse_path,
    read_field,
)
from greenkube.gitops.contracts import ResourceTarget

TARGET = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api")
CONTAINER_CPU = "/spec/template/spec/containers/[name=api]/resources/requests/cpu"


def _deployment() -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "payments"},
        "spec": {"template": {"spec": {"containers": [{"name": "api"}, {"name": "sidecar"}]}}},
    }


def test_paths_round_trip_with_selectors_and_escapes() -> None:
    segments = parse_path("/metadata/annotations/example.com~1team/[name=a~1b]/x~0y")
    assert segments == ("metadata", "annotations", "example.com/team", ListSelector("name", "a/b"), "x~y")
    assert format_path(segments) == "/metadata/annotations/example.com~1team/[name=a~1b]/x~0y"


@pytest.mark.parametrize("path", ["", "/", "spec", "/spec//x", "/spec/[name]", "/spec/[=x]"])
def test_invalid_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        parse_path(path)
    with pytest.raises(ValidationError):
        SetField(ref=TARGET, path=path, value=1)


def test_set_field_creates_mappings_after_the_selector_only() -> None:
    manifest = _deployment()
    apply_field_ops(manifest, [SetField(ref=TARGET, path=CONTAINER_CPU, value="250m")])

    containers = manifest["spec"]["template"]["spec"]["containers"]
    assert containers[0] == {"name": "api", "resources": {"requests": {"cpu": "250m"}}}
    assert containers[1] == {"name": "sidecar"}
    assert read_field(manifest, CONTAINER_CPU) == (True, "250m")


def test_set_field_never_creates_a_list_element() -> None:
    manifest = _deployment()
    with pytest.raises(PrimitiveApplyError, match="container 'worker' must exist exactly once"):
        apply_field_ops(
            manifest,
            [SetField(ref=TARGET, path="/spec/template/spec/containers/[name=worker]/resources/requests/cpu", value=1)],
        )


def test_set_field_requires_the_path_before_the_selector() -> None:
    manifest = {"apiVersion": "apps/v1", "kind": "Deployment", "spec": {}}
    with pytest.raises(PrimitiveApplyError, match="missing"):
        apply_field_ops(manifest, [SetField(ref=TARGET, path=CONTAINER_CPU, value="1")])


def test_non_mapping_intermediate_values_fail_closed() -> None:
    manifest = _deployment()
    manifest["spec"]["template"]["spec"]["containers"][0]["resources"] = "bad"
    with pytest.raises(PrimitiveApplyError, match="container 'api' resources must be a mapping"):
        apply_field_ops(manifest, [SetField(ref=TARGET, path=CONTAINER_CPU, value="1")])


def test_remove_field_is_a_no_op_when_absent_and_removes_when_present() -> None:
    manifest = _deployment()
    apply_field_ops(manifest, [RemoveField(ref=TARGET, path="/spec/replicas")])
    assert "replicas" not in manifest["spec"]
    manifest["spec"]["replicas"] = 3
    apply_field_ops(manifest, [RemoveField(ref=TARGET, path="/spec/replicas")])
    assert "replicas" not in manifest["spec"]


def test_create_resource_exposes_its_identity() -> None:
    primitive = CreateResource(
        manifest={
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": "api", "namespace": "payments"},
        }
    )
    assert primitive.ref == ResourceTarget(
        api_version="autoscaling/v2", kind="HorizontalPodAutoscaler", namespace="payments", name="api"
    )
