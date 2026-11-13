"""The ``delete_resource`` action kind: remove an orphaned resource from Git (roadmap H6).

A deletion pull request removes the resource's manifest from the repository: the whole
file when it holds only that resource, one document of a multi-document file, or the
Kustomize ``resources:`` entry of a single-resource file. The GitOps controller then
prunes the live object (Argo CD ``prune`` or Flux ``prune: true``; otherwise the plan
warns that the deletion will not propagate).

Safety is never weakened here: an action is only built from a recommendation that already
passed the deletion evidence model (selector-less, owned and gateway/mesh/ingress
Services, EndpointSlices, route references, the minimum unused period and the
``greenkube.io/ignore``/``greenkube.io/keep`` opt-outs are filtered by the collectors).
Planning re-checks the manifest in Git: an opted-out resource, a Service that is not a
``LoadBalancer`` and an infrastructure namespace are refused. A deletion is high risk,
never auto-approved and has no automated rollback (a revert recreates the manifest,
not the data of a volume or the address of a load balancer).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from greenkube.automation.actions.base import (
    ActionBaseline,
    ActionKindError,
    ActionKindSpec,
    AppliedVerdict,
    BindingScope,
    InverseUnsupportedError,
    PrSection,
    RiskClass,
    Support,
)
from greenkube.automation.actions.primitives import DeleteResource as DeletePrimitive
from greenkube.automation.actions.primitives import PrimitiveOp
from greenkube.core.deletion_safety import is_infrastructure_namespace, opt_out_reason
from greenkube.gitops.contracts import (
    ActionPlanV3,
    DeleteResource,
    RendererKind,
    ResourceTarget,
    TypedOperation,
)

if TYPE_CHECKING:
    from greenkube.models.metrics import Recommendation

#: Evidence key of a resource binding source: whether the managing controller prunes.
CONTROLLER_PRUNE_EVIDENCE = "controller_prune"


@dataclass(frozen=True, slots=True)
class DeletableTarget:
    """How one recommendation type addresses the resource it proposes to delete."""

    api_version: str
    kind: str
    namespaced: bool
    noun: str


#: Recommendation types whose resource GreenKube can delete through a pull request.
#: Zombie workloads and idle namespaces stay manual: deleting a Namespace manifest
#: removes everything in it, and a zombie owner may be recreated by its own controller.
DELETABLE_TARGETS: Mapping[str, DeletableTarget] = {
    "ORPHANED_LOAD_BALANCER": DeletableTarget("v1", "Service", True, "LoadBalancer Service"),
    "ORPHANED_PERSISTENT_VOLUME": DeletableTarget("v1", "PersistentVolume", False, "PersistentVolume"),
}


def _type_text(record: Any) -> str:
    value = getattr(record, "type", None)
    return str(getattr(value, "value", value) or "")


def deletion_target_for(record: Any) -> ResourceTarget | None:
    """The resource a deletion recommendation proposes to delete, or ``None``."""
    deletable = DELETABLE_TARGETS.get(_type_text(record))
    name = getattr(record, "pod_name", None)
    namespace = getattr(record, "namespace", None) or ""
    if deletable is None or not isinstance(name, str) or not name or name != name.strip():
        return None
    if deletable.namespaced and (not namespace or namespace != namespace.strip()):
        return None
    return ResourceTarget(
        api_version=deletable.api_version,
        kind=deletable.kind,
        namespace=namespace if deletable.namespaced else "",
        name=name,
    )


def describe_target(target: ResourceTarget) -> str:
    """``Service shop/frontend-lb`` or ``PersistentVolume pv-1``."""
    return f"{target.kind} {target.namespace + '/' if target.namespace else ''}{target.name}"


class DeleteResourceSpec(ActionKindSpec):
    """``delete_resource``: delete an orphaned LoadBalancer Service or PersistentVolume from Git."""

    kind: ClassVar[str] = "delete_resource"
    operation_model: ClassVar[type[TypedOperation]] = DeleteResource
    capability: ClassVar[str] = "cleanup"
    risk_class: ClassVar[RiskClass] = "high"
    measurement_strategy: ClassVar[str] = "delete_resource"
    pr_label: ClassVar[str] = "cleanup"
    pr_subject: ClassVar[str] = "resource"
    title: ClassVar[str] = "resource deletion"
    description: ClassVar[str] = "resource deletion"
    binding_scope: ClassVar[BindingScope] = "resource"
    resource_kinds: ClassVar[frozenset[tuple[str, str]]] = frozenset(
        ("", target.kind) for target in DELETABLE_TARGETS.values()
    )
    auto_approvable: ClassVar[bool] = False

    # -- recommendations ---------------------------------------------------------------
    def ops_for(self, recommendations: Sequence[Recommendation]) -> dict[int, ActionPlanV3]:
        actions: dict[int, ActionPlanV3] = {}
        for recommendation in recommendations:
            recommendation_id = recommendation.id
            target = deletion_target_for(recommendation)
            if recommendation_id is None or recommendation_id <= 0 or target is None:
                continue
            constraints: dict[str, Any] = {"resource_deletion": True, "recommendation_type": _type_text(recommendation)}
            payload = {"target": target.model_dump(mode="json"), "recommendation_ids": [recommendation_id]}
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            actions[recommendation_id] = ActionPlanV3(
                action_id=f"action-{digest[:20]}",
                recommendation_ids=[recommendation_id],
                target=target,
                operations=[DeleteResource()],
                constraints=constraints,
                evidence_ref=f"recommendation-evidence/{recommendation_id}",
            )
        return actions

    def matches_recommendation(self, record: Any, target: ResourceTarget) -> bool:
        return deletion_target_for(record) == target

    # -- validation and compilation ------------------------------------------------------
    def validate_operations(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> None:
        if len(operations) != 1 or not isinstance(operations[0], DeleteResource):
            raise ActionKindError("a resource deletion has exactly one delete_resource operation")
        deletable = next(
            (
                item
                for item in DELETABLE_TARGETS.values()
                if (item.api_version, item.kind) == (target.api_version, target.kind)
            ),
            None,
        )
        if deletable is None:
            raise ActionKindError(f"GreenKube does not delete {target.api_version}/{target.kind} resources")
        if deletable.namespaced != bool(target.namespace):
            raise ActionKindError(
                f"{target.kind} deletion targets must {'' if deletable.namespaced else 'not '}name a namespace"
            )
        if target.namespace and is_infrastructure_namespace(target.namespace):
            raise ActionKindError(
                f"GreenKube never deletes resources of the infrastructure namespace {target.namespace!r}"
            )

    def compile(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> list[PrimitiveOp]:
        self.validate_operations(target, operations)
        return [DeletePrimitive(ref=target)]

    def validate_target(self, manifest: Mapping[str, Any], target: ResourceTarget) -> None:
        """The manifest in Git must still be a resource GreenKube may delete."""
        metadata = manifest.get("metadata")
        reason = opt_out_reason(metadata if isinstance(metadata, Mapping) else None)
        if reason is not None:
            raise ActionKindError(f"{describe_target(target)} is opted out of deletion ({reason})")
        if target.kind == "Service":
            spec = manifest.get("spec")
            service_type = spec.get("type") if isinstance(spec, Mapping) else None
            if service_type != "LoadBalancer":
                raise ActionKindError(f"{describe_target(target)} is not a LoadBalancer Service in Git")

    def renderer_support(self, renderer: RendererKind, operations: Sequence[TypedOperation]) -> Support:
        del operations
        if renderer in (RendererKind.RAW, RendererKind.KUSTOMIZE):
            return Support.supported()
        return Support.unsupported(
            "a chart-rendered resource can only be removed through a values toggle; delete it in the chart "
            "or apply the deletion manually"
        )

    def planning_warnings(
        self, manifest: Mapping[str, Any], target: ResourceTarget, evidence: Mapping[str, Any]
    ) -> list[str]:
        """Warnings the deletion pull request must carry (prune disabled, retained volume)."""
        warnings: list[str] = []
        if evidence.get(CONTROLLER_PRUNE_EVIDENCE) is not True:
            warnings.append(
                f"deletion_prune_disabled: the GitOps controller managing {describe_target(target)} does not prune "
                "(Argo CD syncPolicy.automated.prune or Flux spec.prune is not enabled); removing the manifest "
                "will not delete the live object until it is pruned or deleted by hand"
            )
        if target.kind == "PersistentVolume":
            spec = manifest.get("spec")
            policy = spec.get("persistentVolumeReclaimPolicy") if isinstance(spec, Mapping) else None
            if policy in (None, "Retain"):
                warnings.append(
                    f"persistent_volume_retained: {describe_target(target)} has reclaimPolicy "
                    f"{policy or 'Retain (default for static volumes)'}; deleting the PersistentVolume does not "
                    "release the cloud disk, which must be deleted manually to stop its cost"
                )
        return warnings

    # -- baseline, apply detection, inverse ------------------------------------------------
    def baseline_covers(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> bool:
        del operations, baseline
        return True

    def capture_baseline(self, live: Mapping[str, Any] | None, target: ResourceTarget) -> ActionBaseline | None:
        if live is None:
            raise ActionKindError(f"no_effective_change: {describe_target(target)} is already absent from the cluster")
        metadata = live.get("metadata")
        reason = opt_out_reason(metadata if isinstance(metadata, Mapping) else None)
        if reason is not None:
            raise ActionKindError(f"{describe_target(target)} is opted out of deletion in the cluster ({reason})")
        return None

    def is_resource_applied(self, live: Mapping[str, Any] | None, target: ResourceTarget) -> AppliedVerdict:
        """A deletion is applied once the resource is absent from the cluster."""
        if live is None:
            return AppliedVerdict(applied=True, expected="absent", observed="absent")
        return AppliedVerdict(
            applied=False,
            expected="absent",
            observed="present",
            reason=f"{describe_target(target)} still exists",
        )

    def validate_inverse_scope(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> None:
        del operations, baseline
        raise InverseUnsupportedError(
            "a deletion has no automated rollback: reverting the commit recreates the manifest, not the volume's "
            "data or the load balancer's address"
        )

    def inverse(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> list[TypedOperation]:
        self.validate_inverse_scope(operations, baseline)
        return []

    def supports_rollback(self, operations: Sequence[TypedOperation]) -> bool:
        del operations
        return False

    def pr_section(self, operations: Sequence[TypedOperation], recommendation: Any = None) -> PrSection:
        del operations
        target = deletion_target_for(recommendation) if recommendation is not None else None
        lines = [f"Delete {describe_target(target)} from the repository"] if target is not None else ["Delete resource"]
        return PrSection(label=self.pr_label, title="Resource deletion", lines=tuple(lines))


DELETE_RESOURCE = DeleteResourceSpec()
