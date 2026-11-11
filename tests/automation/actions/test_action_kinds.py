"""Action-kind registry and a test-only kind planned end to end (H0a, H0b acceptance)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest
from pydantic import ValidationError

from greenkube.automation.actions import (
    ACTION_KINDS,
    CONTAINER_RESOURCES,
    ActionKindSpec,
    CreateResource,
    UnknownActionKindError,
    action_kind_of,
    operation_subjects,
)
from greenkube.automation.rollback import UnsupportedRollbackError, build_inverse_plan
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AddHorizontalPodAutoscaler,
    ContainerResources,
    DeleteResource,
    Plan,
    PlanStatus,
    ResourceTarget,
    SetContainerResources,
    TypedOperation,
)
from greenkube.gitops.planning.kustomize import KustomizePlanError, KustomizePlanner
from greenkube.gitops.planning.raw_manifest import RawManifestPlanError
from tests.automation.actions.noop_kind import SetMinReadySeconds, registered  # pyrefly: ignore[missing-import]
from tests.gitops.planning import test_kustomize as kustomize_cases  # pyrefly: ignore[missing-import]
from tests.gitops.planning import test_raw_manifest as raw_cases  # pyrefly: ignore[missing-import]

OPERATION = {"op": "test_set_min_ready_seconds", "seconds": 30}


def _payload(operation: dict[str, Any] = OPERATION) -> dict[str, Any]:
    action = raw_cases._action().model_dump(mode="json")
    action["operations"] = [operation]
    return action


def test_container_resources_is_the_built_in_kind() -> None:
    action = raw_cases._action()
    spec = ACTION_KINDS.spec_for_action(action)

    assert spec is CONTAINER_RESOURCES
    assert spec.kind == "container_resources"
    assert spec.op == "set_container_resources"
    assert operation_subjects(action) == {"api"}


@pytest.mark.parametrize("operation", [DeleteResource(), AddHorizontalPodAutoscaler(min_replicas=1, max_replicas=2)])
def test_operations_without_a_kind_are_explained(operation: Any) -> None:
    action = raw_cases._action(operations=[operation])

    with pytest.raises(UnknownActionKindError, match="only per-container rightsizing operations are supported"):
        ACTION_KINDS.spec_for_action(action)
    assert action_kind_of(action) is None
    assert operation_subjects(action) == set()


def test_unregistered_operations_do_not_parse() -> None:
    with pytest.raises(ValidationError):
        ActionPlanV3.model_validate(_payload())


def test_a_registered_kind_parses_and_round_trips_without_contract_changes() -> None:
    with registered() as spec:
        action = ActionPlanV3.model_validate(_payload())
        assert isinstance(action.operations[0], SetMinReadySeconds)
        assert ACTION_KINDS.spec_for_action(action) is spec
        dumped = action.model_dump(mode="json")
        assert dumped["operations"] == [OPERATION]
        assert ActionPlanV3.model_validate_json(action.model_dump_json()) == action

        with pytest.raises(ValidationError, match="minReadySeconds"):
            ActionPlanV3.model_validate(
                {**_payload(), "operations": [OPERATION, {**OPERATION, "seconds": 5}]},
            )


def test_one_action_cannot_mix_kinds() -> None:
    with registered():
        mixed = ActionPlanV3.model_validate(
            {
                **_payload(),
                "operations": [
                    OPERATION,
                    SetContainerResources(
                        container="api", resources=ContainerResources(requests={"cpu": "100m"})
                    ).model_dump(mode="json"),
                ],
            }
        )
        with pytest.raises(UnknownActionKindError, match="single action kind"):
            ACTION_KINDS.spec_for_action(mixed)


def test_registering_a_second_kind_for_the_same_operation_is_refused() -> None:
    with registered() as spec:
        duplicate = type(spec)()
        with pytest.raises(ValueError, match="already registered"):
            ACTION_KINDS.register(duplicate)


def test_the_raw_strategy_plans_a_test_only_kind() -> None:
    with registered():
        action = ActionPlanV3.model_validate(_payload())
        plan = raw_cases._plan(action=action)

    assert len(plan.files) == 1
    assert "+  minReadySeconds: 30\n" in plan.diff
    assert '"minReadySeconds": 30' in plan.rendered_diff
    assert plan.validation.passed


def test_the_raw_strategy_enforces_the_kind_invariants() -> None:
    with registered():
        action = ActionPlanV3.model_validate(_payload({**OPERATION, "seconds": 7200}))
        with pytest.raises(RawManifestPlanError, match="at most one hour"):
            raw_cases._plan(action=action)


class _MergeRunner(kustomize_cases._FakeKustomizeRunner):
    """Applies strategic-merge patches generically (mappings merged, named list entries merged)."""

    @staticmethod
    def _apply_patch(resources: list[dict[str, Any]], patch: dict[str, Any], target: dict[str, str]) -> None:
        match = next(
            resource
            for resource in resources
            if resource["kind"] == target["kind"] and resource["metadata"]["name"] == target["name"]
        )
        _merge(match["spec"], patch["spec"])


def _merge(base: Any, patch: Any) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        elif isinstance(value, list) and isinstance(base.get(key), list):
            for item in value:
                existing = next(entry for entry in base[key] if entry.get("name") == item["name"])
                _merge(existing, item)
        else:
            base[key] = value


def _kustomize_plan(tmp_path: Path, files: dict[str, str], action: ActionPlanV3) -> Any:
    snapshot = kustomize_cases._snapshot(tmp_path, files)
    try:
        return KustomizePlanner(runner=_MergeRunner()).plan(
            action, kustomize_cases._source(), snapshot, kustomize_cases.BASE_REVISION
        )
    finally:
        snapshot.close()


def test_the_kustomize_strategy_plans_a_test_only_kind_next_to_a_managed_patch(tmp_path: Path) -> None:
    files = dict(kustomize_cases.BASE_FILES)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _kustomize_plan(first_dir, files, kustomize_cases._action())
    for edit in first.files:
        assert edit.content is not None
        files[edit.path] = edit.content

    with registered():
        action = kustomize_cases._action().model_copy(
            update={"operations": [SetMinReadySeconds(seconds=30)], "action_id": "act-2"}
        )
        second = _kustomize_plan(second_dir, files, action)

    (patch,) = [edit for edit in second.files if edit.path.endswith(".yaml") and "kustomization" not in edit.path]
    assert patch.content is not None
    # The container requests the first plan wrote are preserved; the new field is merged in.
    assert "minReadySeconds: 30\n" in patch.content
    assert 'cpu: "300m"' in patch.content
    assert second.validation.passed


def test_a_managed_patch_with_a_field_no_kind_owns_fails_closed(tmp_path: Path) -> None:
    files = dict(kustomize_cases.BASE_FILES)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    with registered():
        action = kustomize_cases._action().model_copy(update={"operations": [SetMinReadySeconds(seconds=30)]})
        first = _kustomize_plan(first_dir, files, action)
    for edit in first.files:
        assert edit.content is not None
        files[edit.path] = edit.content

    # Without the kind registered, its field in the managed patch is no longer GreenKube-managed.
    with pytest.raises(KustomizePlanError, match="does not manage: /spec/minReadySeconds"):
        _kustomize_plan(second_dir, files, kustomize_cases._action())


def test_rollback_explains_that_a_kind_without_an_inverse_cannot_be_rolled_back() -> None:
    files = {"apps/payments/deployment.yaml": raw_cases.DEPLOYMENT}
    with registered():
        action = ActionPlanV3.model_validate(_payload())
        forward = raw_cases._plan(action=action)
        plan = Plan(
            plan_id=forward.plan_id,
            recommendation_ids=action.recommendation_ids,
            source=raw_cases._source(),
            action=action,
            file_plan=forward,
            status=PlanStatus.APPLIED,
            digest=forward.digest,
        )
        updated = {path: forward.files[0].content or "" for path in files}
        with pytest.raises(UnsupportedRollbackError, match="baseline-derived inverse"):
            build_inverse_plan(plan, files, updated, "b" * 40)


class _CreateHpa(TypedOperation):
    op: Literal["test_create_hpa"] = "test_create_hpa"


class _CreateHpaSpec(ActionKindSpec):
    kind: ClassVar[str] = "test_create_hpa"
    operation_model: ClassVar[type[TypedOperation]] = _CreateHpa
    capability: ClassVar[str] = "test"

    def compile(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> list[Any]:
        return [
            CreateResource(
                manifest={
                    "apiVersion": "autoscaling/v2",
                    "kind": "HorizontalPodAutoscaler",
                    "metadata": {"name": target.name, "namespace": target.namespace},
                }
            )
        ]


def test_strategies_refuse_primitives_they_cannot_express_yet(tmp_path: Path) -> None:
    spec = _CreateHpaSpec()
    ACTION_KINDS.register(spec)
    try:
        action = raw_cases._action(operations=[_CreateHpa()])
        with pytest.raises(RawManifestPlanError, match="does not support 'create' primitives yet"):
            raw_cases._plan(action=action)
        with pytest.raises(KustomizePlanError, match="does not support 'create' primitives yet"):
            _kustomize_plan(tmp_path, dict(kustomize_cases.BASE_FILES), action)
    finally:
        ACTION_KINDS.unregister(spec.kind)
