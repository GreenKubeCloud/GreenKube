"""A test-only action kind defined entirely outside ``greenkube`` (H0 acceptance).

It sets ``spec.minReadySeconds`` on a Deployment. Nothing in ``src/`` knows about it:
registering it must be enough for actions to parse, persist, plan (raw and Kustomize),
validate, and be refused by rollback with an explanation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, ClassVar, Literal

from pydantic import Field

from greenkube.automation.actions import ACTION_KINDS, ActionKindError, ActionKindSpec, SetField
from greenkube.automation.actions.primitives import PathSegment
from greenkube.gitops.contracts import ResourceTarget, TypedOperation

PATH = ("spec", "minReadySeconds")


class SetMinReadySeconds(TypedOperation):
    op: Literal["test_set_min_ready_seconds"] = "test_set_min_ready_seconds"
    seconds: int = Field(ge=0)

    def conflict_key(self) -> str | None:
        return "minReadySeconds"


class MinReadySecondsSpec(ActionKindSpec):
    kind: ClassVar[str] = "test_min_ready_seconds"
    operation_model: ClassVar[type[TypedOperation]] = SetMinReadySeconds
    capability: ClassVar[str] = "test"
    title: ClassVar[str] = "test minReadySeconds"
    description: ClassVar[str] = "minReadySeconds"

    def validate_operations(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> None:
        if target.kind != "Deployment":
            raise ActionKindError("minReadySeconds is set on Deployments only")

    def compile(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> list[Any]:
        return [
            SetField(ref=target, path="/spec/minReadySeconds", value=operation.seconds)
            for operation in operations
            if isinstance(operation, SetMinReadySeconds)
        ]

    def owns_patch_field(self, kind: str, segments: Sequence[PathSegment], value: Any) -> bool:
        return kind == "Deployment" and tuple(segments) == PATH and isinstance(value, int)

    def check_invariants(
        self, manifest: Mapping[str, Any], target: ResourceTarget, operations: Sequence[TypedOperation]
    ) -> None:
        value = manifest.get("spec", {}).get("minReadySeconds")
        if not isinstance(value, int) or value > 3600:
            raise ActionKindError("minReadySeconds must be an integer of at most one hour")


MIN_READY_SECONDS = MinReadySecondsSpec()


@contextmanager
def registered() -> Iterator[MinReadySecondsSpec]:
    ACTION_KINDS.register(MIN_READY_SECONDS)
    try:
        yield MIN_READY_SECONDS
    finally:
        ACTION_KINDS.unregister(MIN_READY_SECONDS.kind)
