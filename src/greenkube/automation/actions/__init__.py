"""Action framework (H0): action kinds, primitive operations and patch strategies.

Importing this package registers the built-in action kinds. Planners, render
validation, rollback, apply detection and the plan worker look kinds up through
``ACTION_KINDS`` instead of testing operation types, so a new kind is one
``ActionKindSpec`` subclass registered with ``register_action_kind``.
"""

from greenkube.automation.actions.base import (
    ActionBaseline,
    ActionInvariantError,
    ActionKindError,
    ActionKindSpec,
    AppliedVerdict,
    InverseConflictError,
    InverseUnsupportedError,
    PatchStrategy,
    PrFieldChange,
    PrSection,
    Support,
    UnknownActionKindError,
)
from greenkube.automation.actions.container_resources import CONTAINER_RESOURCES, ContainerResourcesSpec
from greenkube.automation.actions.primitives import (
    CreateResource,
    DeleteResource,
    ListSelector,
    PlacementHint,
    PrimitiveApplyError,
    PrimitiveOp,
    RemoveField,
    ResourceRef,
    SetField,
)
from greenkube.automation.actions.registry import (
    ACTION_KINDS,
    ActionKindRegistry,
    action_kind_of,
    operation_kind_of,
    operation_subjects,
    register_action_kind,
    spec_for_action,
    spec_for_operation,
)

register_action_kind(CONTAINER_RESOURCES)

__all__ = [
    "ACTION_KINDS",
    "CONTAINER_RESOURCES",
    "ActionBaseline",
    "ActionInvariantError",
    "ActionKindError",
    "ActionKindRegistry",
    "ActionKindSpec",
    "AppliedVerdict",
    "ContainerResourcesSpec",
    "CreateResource",
    "DeleteResource",
    "InverseConflictError",
    "InverseUnsupportedError",
    "ListSelector",
    "PatchStrategy",
    "PlacementHint",
    "PrFieldChange",
    "PrSection",
    "PrimitiveApplyError",
    "PrimitiveOp",
    "RemoveField",
    "ResourceRef",
    "SetField",
    "Support",
    "UnknownActionKindError",
    "action_kind_of",
    "operation_kind_of",
    "operation_subjects",
    "register_action_kind",
    "spec_for_action",
    "spec_for_operation",
]
