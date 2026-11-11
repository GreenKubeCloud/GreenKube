"""The container_resources kind: baseline coverage, apply detection, inverse and policy fields (H0c)."""

from __future__ import annotations

import pytest

from greenkube.automation.actions import (
    CONTAINER_RESOURCES,
    ActionKindError,
    InverseConflictError,
    InverseUnsupportedError,
    SetField,
)
from greenkube.gitops.contracts import (
    ContainerRequestBaseline,
    ContainerResources,
    PreApplyWorkloadBaseline,
    ResourceTarget,
    SetContainerResources,
)

TARGET = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api")


def _op(container: str = "api", **resources: dict[str, str]) -> SetContainerResources:
    return SetContainerResources(container=container, resources=ContainerResources(**resources))


def _baseline(cpu: int | None = 500, memory: int | None = 512 * 1024**2) -> PreApplyWorkloadBaseline:
    return PreApplyWorkloadBaseline(
        workload_uid="uid-1",
        generation=2,
        observed_generation=2,
        container_requests={"api": ContainerRequestBaseline(cpu_millicores=cpu, memory_bytes=memory)},
    )


def test_compile_addresses_each_request_and_limit_by_container_name() -> None:
    primitives = CONTAINER_RESOURCES.compile(
        TARGET, [_op(requests={"cpu": "250m", "memory": "256Mi"}, limits={"memory": "512Mi"})]
    )

    assert all(isinstance(primitive, SetField) for primitive in primitives)
    assert [
        (primitive.op, primitive.path, primitive.value) for primitive in primitives if isinstance(primitive, SetField)
    ] == [
        ("set", "/spec/template/spec/containers/[name=api]/resources/requests/cpu", "250m"),
        ("set", "/spec/template/spec/containers/[name=api]/resources/requests/memory", "256Mi"),
        ("set", "/spec/template/spec/containers/[name=api]/resources/limits/memory", "512Mi"),
    ]


def test_cronjob_targets_compile_through_the_job_template() -> None:
    target = TARGET.model_copy(update={"api_version": "batch/v1", "kind": "CronJob"})
    (primitive,) = CONTAINER_RESOURCES.compile(target, [_op(requests={"cpu": "1"})])

    assert isinstance(primitive, SetField)
    assert primitive.path == "/spec/jobTemplate/spec/template/spec/containers/[name=api]/resources/requests/cpu"


def test_validate_operations_rejects_unsupported_targets_and_empty_operations() -> None:
    with pytest.raises(ActionKindError, match="unsupported workload kind"):
        CONTAINER_RESOURCES.validate_operations(TARGET.model_copy(update={"api_version": "v1"}), [_op()])
    with pytest.raises(ActionKindError, match="at least one resource"):
        CONTAINER_RESOURCES.validate_operations(TARGET, [_op()])


def test_effective_change_normalizes_units_against_the_live_baseline() -> None:
    same = [_op(requests={"cpu": "0.5", "memory": "512Mi"})]
    changed = [_op(requests={"cpu": "250m"})]

    assert CONTAINER_RESOURCES.effective_change(same, _baseline()) is False
    assert CONTAINER_RESOURCES.effective_change(changed, _baseline()) is True
    assert CONTAINER_RESOURCES.effective_change([_op("other", requests={"cpu": "1"})], _baseline()) is None
    assert CONTAINER_RESOURCES.effective_change([_op(limits={"cpu": "1"})], _baseline()) is None


def test_is_applied_compares_the_observed_request_within_tolerance() -> None:
    operations = [_op(requests={"cpu": "250m"})]

    applied = CONTAINER_RESOURCES.is_applied(operations, subject="api", field_name="cpu", observed=255, tolerance=0.05)
    drifted = CONTAINER_RESOURCES.is_applied(operations, subject="api", field_name="cpu", observed=400, tolerance=0.05)
    unplanned = CONTAINER_RESOURCES.is_applied(
        operations, subject="api", field_name="memory", observed=1, tolerance=0.05
    )

    assert applied.applied and applied.expected == 250
    assert not drifted.applied
    assert not unplanned.applied


def test_inverse_restores_the_persisted_requests() -> None:
    (inverse,) = CONTAINER_RESOURCES.inverse([_op(requests={"cpu": "250m", "memory": "256Mi"})], _baseline())

    assert inverse == _op(requests={"cpu": "500m", "memory": str(512 * 1024**2)})


@pytest.mark.parametrize(
    ("operations", "baseline", "error"),
    [
        ([_op(requests={"cpu": "1"}, limits={"cpu": "2"})], _baseline(), InverseUnsupportedError),
        ([_op(requests={"ephemeral-storage": "1Gi"})], _baseline(), InverseUnsupportedError),
        ([_op("other", requests={"cpu": "1"})], _baseline(), InverseConflictError),
        ([_op(requests={"cpu": "1"})], _baseline(cpu=None), InverseConflictError),
    ],
)
def test_inverse_fails_closed(operations, baseline, error) -> None:
    with pytest.raises(error):
        CONTAINER_RESOURCES.inverse(operations, baseline)


def test_inverse_scope_does_not_require_baseline_values() -> None:
    """Whole-file restorations (Flux HelmRelease) only need the scope, not derived values."""
    CONTAINER_RESOURCES.validate_inverse_scope([_op(requests={"cpu": "1"})], _baseline(cpu=None))


def test_changed_fields_feed_policy_gates() -> None:
    fields = CONTAINER_RESOURCES.changed_fields([_op(requests={"CPU": "1"}, limits={"memory": "1Gi"})])

    assert fields == {("api", "cpu"), ("api", "memory")}
