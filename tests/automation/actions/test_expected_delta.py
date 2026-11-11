"""Generic expected-delta render validation (H0c)."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, ClassVar, Literal

import pytest

from greenkube.automation.actions import CONTAINER_RESOURCES, ActionKindSpec, CreateResource, SetField
from greenkube.automation.actions.validation import ExpectedDeltaError, resource_identity, validate_expected_delta
from greenkube.gitops.contracts import ContainerResources, ResourceTarget, SetContainerResources, TypedOperation

TARGET = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api")
SERVICE = ResourceTarget(api_version="v1", kind="Service", namespace="payments", name="api")
HPA = {
    "apiVersion": "autoscaling/v2",
    "kind": "HorizontalPodAutoscaler",
    "metadata": {"name": "api", "namespace": "payments"},
    "spec": {"minReplicas": 2, "maxReplicas": 4},
}


def _deployment(cpu: str = "500m", checksum: str | None = None) -> dict[str, Any]:
    template: dict[str, Any] = {"spec": {"containers": [{"name": "api", "resources": {"requests": {"cpu": cpu}}}]}}
    if checksum is not None:
        template["metadata"] = {"annotations": {"checksum/config": checksum}}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api"}, "spec": {"template": template}}


def _rendered(deployment: dict[str, Any], *extra: dict[str, Any]) -> dict[tuple[str, str, str, str], Any]:
    resources = {resource_identity(TARGET): deployment, resource_identity(SERVICE): {"kind": "Service"}}
    for manifest in extra:
        resources[CreateResource(manifest=manifest).ref.api_version, manifest["kind"], "payments", "api"] = manifest
    return resources


OPERATIONS = [SetContainerResources(container="api", resources=ContainerResources(requests={"cpu": "250m"}))]


def _validate(before: Any, after: Any, spec: ActionKindSpec = CONTAINER_RESOURCES, operations: Any = None) -> None:
    validate_expected_delta(
        before,
        after,
        spec=spec,
        target=TARGET,
        target_identity=resource_identity(TARGET),
        operations=OPERATIONS if operations is None else operations,
    )


def test_exactly_the_expected_field_change_passes() -> None:
    _validate(_rendered(_deployment()), _rendered(_deployment("250m")))


def test_recomputed_checksum_annotations_are_tolerated() -> None:
    _validate(_rendered(_deployment(checksum="a")), _rendered(_deployment("250m", checksum="b")))


def test_an_unrelated_resource_change_fails() -> None:
    after = _rendered(_deployment("250m"))
    after[resource_identity(SERVICE)] = {"kind": "Service", "changed": True}

    with pytest.raises(ExpectedDeltaError, match="unrelated resource change"):
        _validate(_rendered(_deployment()), after)


def test_a_change_outside_the_planned_fields_fails() -> None:
    after = _rendered(_deployment("250m"))
    after[resource_identity(TARGET)]["spec"]["replicas"] = 3

    with pytest.raises(ExpectedDeltaError, match="outside the requested container resources"):
        _validate(_rendered(_deployment()), after)


def test_a_missing_field_change_fails() -> None:
    with pytest.raises(ExpectedDeltaError, match="outside the requested"):
        _validate(_rendered(_deployment()), _rendered(_deployment()))


class _AddHpa(TypedOperation):
    op: Literal["test_add_hpa"] = "test_add_hpa"


class _HpaSpec(ActionKindSpec):
    kind: ClassVar[str] = "test_hpa"
    operation_model: ClassVar[type[TypedOperation]] = _AddHpa
    capability: ClassVar[str] = "test"
    description: ClassVar[str] = "autoscaler"

    def compile(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> list[Any]:
        return [
            CreateResource(manifest=copy.deepcopy(HPA)),
            SetField(ref=target, path="/metadata/annotations", value={"autoscaled": "true"}),
        ]


def test_created_resources_are_part_of_the_expected_delta() -> None:
    """Creating a resource is no longer an unrelated change when the kind declares it."""
    after_deployment = _deployment()
    after_deployment["metadata"]["annotations"] = {"autoscaled": "true"}

    _validate(_rendered(_deployment()), _rendered(after_deployment, HPA), spec=_HpaSpec(), operations=[_AddHpa()])

    with pytest.raises(ExpectedDeltaError, match="added or removed"):
        _validate(_rendered(_deployment()), _rendered(after_deployment), spec=_HpaSpec(), operations=[_AddHpa()])
    with pytest.raises(ExpectedDeltaError, match="added or removed"):
        _validate(_rendered(_deployment()), _rendered(_deployment("250m"), HPA))
