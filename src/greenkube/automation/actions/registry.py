"""The action-kind registry: typed operation -> ``ActionKindSpec`` (H0a)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from greenkube.automation.actions.base import ActionKindSpec, UnknownActionKindError
from greenkube.automation.actions.primitives import PathSegment
from greenkube.gitops.contracts import (
    ActionPlanV3,
    TypedOperation,
    register_operation_model,
    unregister_operation_model,
)

if TYPE_CHECKING:
    from greenkube.models.metrics import Recommendation


class ActionKindRegistry:
    """Registered action kinds, looked up by kind name or by typed-operation ``op``."""

    def __init__(self) -> None:
        self._by_kind: dict[str, ActionKindSpec] = {}
        self._by_op: dict[str, ActionKindSpec] = {}

    def register(self, spec: ActionKindSpec) -> ActionKindSpec:
        """Register ``spec`` and make its typed operation parseable in ``ActionPlanV3``."""
        if not isinstance(spec, ActionKindSpec):
            raise TypeError("action kinds must be ActionKindSpec instances")
        kind, op = spec.kind, spec.op
        existing = self._by_kind.get(kind) or self._by_op.get(op)
        if existing is not None:
            if existing is spec:
                return spec
            raise ValueError(f"action kind {kind!r} (operation {op!r}) is already registered")
        register_operation_model(op, spec.operation_model)
        self._by_kind[kind] = spec
        self._by_op[op] = spec
        return spec

    def unregister(self, kind: str) -> None:
        """Remove a kind (tests only)."""
        spec = self._by_kind.pop(kind, None)
        if spec is not None:
            self._by_op.pop(spec.op, None)
            unregister_operation_model(spec.op)

    def __iter__(self) -> Iterator[ActionKindSpec]:
        return iter(tuple(self._by_kind.values()))

    def __contains__(self, kind: object) -> bool:
        return kind in self._by_kind

    def get(self, kind: str) -> ActionKindSpec | None:
        return self._by_kind.get(kind)

    def spec_for_operation(self, operation: TypedOperation) -> ActionKindSpec:
        spec = self._by_op.get(str(getattr(operation, "op", "")))
        if spec is None or not isinstance(operation, spec.operation_model):
            op = getattr(operation, "op", type(operation).__name__)
            raise UnknownActionKindError(
                f"operation {op!r} has no registered action kind; only {self.supported_titles()} operations are "
                "supported"
            )
        return spec

    def spec_for_action(self, action: ActionPlanV3) -> ActionKindSpec:
        """The single kind of every operation of ``action`` (mixed or unknown kinds raise)."""
        return self.spec_for_operations(action.operations)

    def spec_for_operations(self, operations: Sequence[TypedOperation]) -> ActionKindSpec:
        """The single kind of ``operations`` (empty, mixed or unknown kinds raise)."""
        if not operations:
            raise UnknownActionKindError(
                f"the action has no operations; only {self.supported_titles()} operations are supported"
            )
        specs = {id(spec): spec for spec in (self.spec_for_operation(op) for op in operations)}
        if len(specs) != 1:
            raise UnknownActionKindError("one action may combine operations of a single action kind only")
        return next(iter(specs.values()))

    def supported_titles(self) -> str:
        """Human list of the registered kinds, for "unsupported operation" messages."""
        return ", ".join(spec.title for spec in self) or "none"

    def kind_of(self, action: ActionPlanV3) -> ActionKindSpec | None:
        """``spec_for_action`` without raising."""
        try:
            return self.spec_for_action(action)
        except UnknownActionKindError:
            return None

    def owns_patch_field(self, kind: str, segments: Sequence[PathSegment], value: Any) -> bool:
        """Whether any registered kind writes this field into a GreenKube-managed patch."""
        return any(spec.owns_patch_field(kind, segments, value) for spec in self)

    def may_express_any(self, recommendations: Sequence[Recommendation]) -> bool:
        """Whether a registered kind may build an action for one of ``recommendations``."""
        return any(spec.may_express(recommendation) for recommendation in recommendations for spec in self)

    def build_action_plans(self, recommendations: Sequence[Recommendation]) -> dict[int, ActionPlanV3]:
        """Each recommendation id -> the action of the first registered kind that expresses it."""
        actions: dict[int, ActionPlanV3] = {}
        for spec in self:
            for recommendation_id, action in spec.ops_for(recommendations).items():
                actions.setdefault(recommendation_id, action)
        return actions


#: The process-wide registry; built-in kinds register on import of ``greenkube.automation.actions``.
ACTION_KINDS = ActionKindRegistry()


def register_action_kind(spec: ActionKindSpec) -> ActionKindSpec:
    return ACTION_KINDS.register(spec)


def spec_for_operation(operation: TypedOperation) -> ActionKindSpec:
    return ACTION_KINDS.spec_for_operation(operation)


def spec_for_action(action: ActionPlanV3) -> ActionKindSpec:
    return ACTION_KINDS.spec_for_action(action)


def action_kind_of(action: ActionPlanV3 | None) -> ActionKindSpec | None:
    """The registered kind of ``action``, or ``None`` when it has none (no action, mixed or unknown)."""
    return ACTION_KINDS.kind_of(action) if action is not None else None


def operation_kind_of(operation: TypedOperation) -> ActionKindSpec | None:
    """The registered kind of one operation, or ``None``."""
    try:
        return ACTION_KINDS.spec_for_operation(operation)
    except UnknownActionKindError:
        return None


def operation_subjects(action: ActionPlanV3) -> set[str]:
    """Sub-resources (container names) an action changes; empty for an unknown kind."""
    spec = action_kind_of(action)
    return spec.subjects(action.operations) if spec is not None else set()
