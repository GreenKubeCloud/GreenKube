"""The action-kind contract (H0a) and the patch-strategy contract (H0b).

An action kind owns everything specific to one kind of change: its typed operation,
how recommendations become operations, which resources it touches, how it compiles to
primitive edits, the invariants a rendered result must keep, its baseline, apply
detection, inverse and pull-request section. Planners, render validation, the plan
worker, rollback and apply detection only call this contract, so a new kind needs no
change outside ``greenkube.automation.actions``.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from greenkube.automation.actions.primitives import PathSegment, PrimitiveOp
from greenkube.gitops.contracts import (
    ActionPlanV3,
    FilePlan,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    TypedOperation,
    WorkloadSource,
)

if TYPE_CHECKING:
    from greenkube.models.metrics import Recommendation

RiskClass = Literal["low", "medium", "high"]

#: The pre-apply baseline a kind captures before its change is delivered. Container
#: resources persist ``PreApplyWorkloadBaseline`` (workload identity + per-container
#: requests); kinds without per-container state only use its workload identity.
ActionBaseline = PreApplyWorkloadBaseline


class ActionKindError(ValueError):
    """An action cannot be handled by the registered action kinds."""


class UnknownActionKindError(ActionKindError):
    """An operation has no registered action kind."""


class ActionInvariantError(ActionKindError):
    """A manifest violates an invariant of the action kind (for example request > limit)."""


class InverseUnsupportedError(ActionKindError):
    """The kind cannot derive an inverse for these operations."""


class InverseConflictError(ActionKindError):
    """The persisted baseline cannot restore the operations exactly."""


@dataclass(frozen=True, slots=True)
class Support:
    """Whether a patch strategy can express one primitive for one binding."""

    state: Literal["ok", "needs_mapping", "unsupported"]
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    @classmethod
    def supported(cls) -> Support:
        return cls("ok")

    @classmethod
    def needs_mapping(cls, reason: str) -> Support:
        return cls("needs_mapping", reason)

    @classmethod
    def unsupported(cls, reason: str) -> Support:
        return cls("unsupported", reason)


@dataclass(frozen=True, slots=True)
class AppliedVerdict:
    """Whether live state shows the planned value (apply detection)."""

    applied: bool
    expected: Any = None
    observed: Any = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PrFieldChange:
    """One field a kind's operations set, as the pull request shows it (``requests.cpu`` -> ``250m``)."""

    #: Sub-resource the field belongs to (a container name), as in :meth:`ActionKindSpec.subjects`.
    subject: str
    #: Dotted field name (``requests.cpu``, ``limits.memory``).
    field: str
    #: The value the operation writes, as written to the manifest.
    proposed: str


@dataclass(frozen=True, slots=True)
class PrSection:
    """What a kind contributes to the pull request: label, title, change lines and structured changes."""

    label: str
    title: str
    lines: tuple[str, ...] = field(default_factory=tuple)
    #: The fields the operations set, in the order the pull request lists them.
    changes: tuple[PrFieldChange, ...] = field(default_factory=tuple)


class ActionKindSpec:
    """Base class of an action kind. Subclasses set the class attributes and override hooks.

    The defaults describe a kind that changes fields of the action target only and has no
    per-container state, baseline values, inverse or apply detection of its own.
    """

    kind: ClassVar[str]
    operation_model: ClassVar[type[TypedOperation]]
    capability: ClassVar[str]
    risk_class: ClassVar[RiskClass] = "low"
    measurement_strategy: ClassVar[str] = "none"
    #: Pull-request label (``pr_content`` category) and the noun used in validation messages.
    pr_label: ClassVar[str] = "change"
    #: Noun of the kind's sub-resources in pull-request text ("container").
    pr_subject: ClassVar[str] = "resource"
    #: Short human name of the kind ("per-container rightsizing").
    title: ClassVar[str] = "change"
    #: What the kind changes, used in validation messages ("container resources").
    description: ClassVar[str] = "action fields"

    @property
    def op(self) -> str:
        """The ``op`` discriminator of this kind's typed operation."""
        default = self.operation_model.model_fields["op"].default
        if not isinstance(default, str):
            raise TypeError(f"{self.operation_model.__name__} must declare a default 'op' literal")
        return default

    # -- recommendations ---------------------------------------------------------------
    def ops_for(self, recommendations: Sequence[Recommendation]) -> dict[int, ActionPlanV3]:
        """Build this kind's actions for the recommendations it can express (recommendation id -> action)."""
        del recommendations
        return {}

    # -- targets and validation ------------------------------------------------------------
    def targets(self, action: ActionPlanV3) -> list[ResourceTarget]:
        """Resources that need a binding (by default the action target only)."""
        return [action.target]

    def gates(self) -> tuple[str, ...]:
        """Names of the policy gates this kind requires."""
        return ()

    def validate_operations(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> None:
        """Reject operations the kind cannot plan for ``target`` (raise ``ActionKindError``)."""
        del target, operations

    def subjects(self, operations: Sequence[TypedOperation]) -> set[str]:
        """Sub-resources the operations change (for example container names)."""
        del operations
        return set()

    def changed_fields(self, operations: Sequence[TypedOperation]) -> set[tuple[str, str]]:
        """``(subject, field)`` pairs the operations change, for policy gates (for example
        ``("api", "cpu")``); empty when the kind has no sub-resources."""
        del operations
        return set()

    def compile(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> list[PrimitiveOp]:
        """Typed operations -> primitive edits of ``target`` (and of created or deleted resources)."""
        raise NotImplementedError

    def validate_target(self, manifest: Mapping[str, Any], target: ResourceTarget) -> None:
        """Check the target manifest has the shape the primitives expect (raise ``ActionKindError``)."""
        del manifest, target

    def check_invariants(
        self,
        manifest: MutableMapping[str, Any],
        target: ResourceTarget,
        operations: Sequence[TypedOperation],
    ) -> None:
        """Per-kind invariants of an edited manifest (raise ``ActionInvariantError``)."""
        del manifest, target, operations

    def owns_patch_field(self, kind: str, segments: Sequence[PathSegment], value: Any) -> bool:
        """Whether a field kept in a GreenKube-managed patch belongs to this kind."""
        del kind, segments, value
        return False

    def renderer_support(self, renderer: RendererKind, operations: Sequence[TypedOperation]) -> Support:
        """Kind-level limits of a renderer (for example Kustomize v1 writes requests only)."""
        del renderer, operations
        return Support.supported()

    # -- baseline, apply detection, inverse ------------------------------------------------
    def baseline_covers(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> bool:
        """Whether a captured or persisted baseline holds everything the kind needs for these operations."""
        return self.subjects(operations).issubset(baseline.container_requests)

    def effective_change(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> bool | None:
        """``False`` when the live baseline already equals every planned value; ``None`` when unknown."""
        del operations, baseline
        return None

    def is_applied(
        self,
        operations: Sequence[TypedOperation],
        *,
        subject: str,
        field_name: str,
        observed: int | None,
        tolerance: float,
    ) -> AppliedVerdict:
        """Whether an observed live value shows the planned value of ``subject``/``field_name``."""
        del operations, subject, field_name
        return AppliedVerdict(applied=False, observed=observed, reason=f"tolerance {tolerance} not applicable")

    def validate_inverse_scope(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> None:
        """Whether rollback can restore these operations from ``baseline`` without deriving values.

        Renderers that restore whole files (Flux HelmRelease) only need this; ``inverse``
        derives the restoring operations. Raise ``InverseUnsupportedError`` or
        ``InverseConflictError``.
        """
        del operations, baseline
        raise InverseUnsupportedError(f"rollback of {self.kind} actions is not supported")

    def inverse(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> list[TypedOperation]:
        """Typed operations restoring ``baseline`` (raise ``InverseUnsupportedError``/``InverseConflictError``)."""
        del operations, baseline
        raise InverseUnsupportedError(f"rollback of {self.kind} actions is not supported")

    def supports_rollback(self, operations: Sequence[TypedOperation]) -> bool:
        """Whether a delivered change of these operations can be rolled back."""
        del operations
        return False

    def pr_section(self, operations: Sequence[TypedOperation], recommendation: Any = None) -> PrSection:
        """The pull-request section describing the operations."""
        del recommendation
        return PrSection(
            label=self.pr_label,
            title=self.kind,
            lines=tuple(str(getattr(operation, "op", type(operation).__name__)) for operation in operations),
        )


class PatchStrategy(Protocol):
    """One per renderer: decides which primitives it can express and plans the file change.

    ``plan`` validates the action through its kind, compiles it to primitives, writes the
    source edits, renders before and after, validates the expected delta and returns the
    digest-bound file plan. Implementations: ``RawManifestPlanner`` (raw),
    ``KustomizePlanner`` (kustomize), ``HelmValuesPlanner`` (helm_values) and
    ``FluxHelmReleasePlanner`` (helm_release post-renderer).
    """

    renderer_kind: RendererKind

    def supports(self, op: PrimitiveOp, binding: WorkloadSource) -> Support: ...

    def plan(self, action: ActionPlanV3, source: WorkloadSource, snapshot: Any, base_revision: str) -> FilePlan: ...
