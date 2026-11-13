"""Generic apply detection and inverse of primitive operations (H0c)."""

from __future__ import annotations

import pytest

from greenkube.automation.actions import (
    CreateResource,
    DeleteResource,
    PrimitiveApplyError,
    RemoveField,
    ResourceRef,
    SetField,
)
from greenkube.automation.actions.primitives import invert_primitives, primitive_applied, primitive_identity

DEPLOY = ResourceRef(api_version="apps/v1", kind="Deployment", namespace="shop", name="api")
HPA_MANIFEST = {
    "apiVersion": "autoscaling/v2",
    "kind": "HorizontalPodAutoscaler",
    "metadata": {"name": "api", "namespace": "shop"},
    "spec": {"minReplicas": 2, "maxReplicas": 5},
}
HPA = ResourceRef(api_version="autoscaling/v2", kind="HorizontalPodAutoscaler", namespace="shop", name="api")
LIVE = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api"}, "spec": {"replicas": 3}}


def test_create_and_delete_are_detected_from_presence() -> None:
    assert primitive_applied(DeleteResource(ref=DEPLOY), None)
    assert not primitive_applied(DeleteResource(ref=DEPLOY), LIVE)
    assert primitive_applied(CreateResource(manifest=HPA_MANIFEST), HPA_MANIFEST)
    assert not primitive_applied(CreateResource(manifest=HPA_MANIFEST), None)


def test_field_edits_are_detected_from_the_live_value() -> None:
    assert primitive_applied(SetField(ref=DEPLOY, path="/spec/replicas", value=3), LIVE)
    assert not primitive_applied(SetField(ref=DEPLOY, path="/spec/replicas", value=2), LIVE)
    assert primitive_applied(RemoveField(ref=DEPLOY, path="/spec/paused"), LIVE)
    assert not primitive_applied(SetField(ref=DEPLOY, path="/spec/replicas", value=3), None)


def test_inverse_restores_created_deleted_and_edited_resources() -> None:
    before = {primitive_identity(DEPLOY): LIVE}
    inverse = invert_primitives(
        [
            CreateResource(manifest=HPA_MANIFEST),
            SetField(ref=DEPLOY, path="/spec/replicas", value=1),
            SetField(ref=DEPLOY, path="/spec/paused", value=True),
            DeleteResource(ref=DEPLOY),
        ],
        before,
    )

    assert isinstance(inverse[0], CreateResource) and inverse[0].manifest == LIVE
    assert inverse[1] == RemoveField(ref=DEPLOY, path="/spec/paused")
    assert inverse[2] == SetField(ref=DEPLOY, path="/spec/replicas", value=3)
    assert inverse[3] == DeleteResource(ref=HPA)


def test_inverse_never_guesses_unrecorded_state() -> None:
    with pytest.raises(PrimitiveApplyError, match="no recorded state"):
        invert_primitives([DeleteResource(ref=DEPLOY)], {})
