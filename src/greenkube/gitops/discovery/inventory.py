"""Resource bindings by GVK and name from Argo CD and Flux inventories (H0d).

Workload bindings (``WorkloadSource``) only cover pod-template workloads. Action kinds
that change other resources (an existing HPA, a PersistentVolume to delete, a KEDA
ScaledObject, a Karpenter NodePool) need to know which GitOps controller manages a
resource of any kind. Both controllers already list every resource they manage:

- an Argo CD ``Application`` in ``status.resources`` (``group``, ``version``, ``kind``,
  ``namespace``, ``name``);
- a Flux ``Kustomization`` in ``status.inventory.entries`` (``id`` =
  ``<namespace>_<name>_<group>_<kind>``, ``v`` = version).

``ResourceBindingIndex`` maps those inventories to ``resource_bindings(gvk, namespace,
name)``. A resource listed by exactly one controller is bound; one listed by several is
ambiguous and an unlisted one is unmanaged, so callers fail closed (or fall back to an
advisory) instead of guessing. The binding carries the controller's resolved Git
location when discovery resolved one of its workloads.

A controller that deploys to another cluster (an Argo CD destination that is not the local
cluster, a Flux object with ``spec.kubeConfig``) lists resources of that cluster: its
entries still make a same-named local resource ambiguous, but never resolve one
(``destination_remote``), so no plan, deletion included, is bound through it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from greenkube.gitops.contracts import BindingStatus, ControllerKind, ResourceTarget, WorkloadSource
from greenkube.gitops.discovery.common import (
    DESTINATION_REMOTE_REASON,
    argo_destination_is_local,
    as_mapping,
    flux_targets_remote_cluster,
    text,
)
from greenkube.gitops.discovery.flux import parse_flux_inventory_id

ControllerIdentity = tuple[ControllerKind, str, str]

#: ``WorkloadSource.namespace`` of a cluster-scoped resource binding (a PersistentVolume).
#: Workload sources require a namespace; a Kubernetes namespace name is a DNS label and can
#: never start with ``_``, so this sentinel never collides with a real namespace.
CLUSTER_SCOPE_NAMESPACE = "_cluster"
#: Evidence key of a resource binding source: whether the managing controller prunes
#: resources removed from Git (Argo CD ``syncPolicy.automated.prune``, Flux ``spec.prune``).
CONTROLLER_PRUNE_EVIDENCE = "controller_prune"
#: Evidence key marking a source derived from a resource binding (not a workload binding).
RESOURCE_BINDING_EVIDENCE = "resource_binding"


def source_namespace(namespace: str) -> str:
    """The ``WorkloadSource.namespace`` of a resource namespace ("" is cluster scope)."""
    return namespace or CLUSTER_SCOPE_NAMESPACE


def target_namespace(source_namespace_value: str) -> str:
    """The resource namespace a ``WorkloadSource.namespace`` stands for."""
    return "" if source_namespace_value == CLUSTER_SCOPE_NAMESPACE else source_namespace_value


def source_matches_target(source: WorkloadSource, target: ResourceTarget) -> bool:
    """Whether a (workload or resource) binding source addresses the action target."""
    return (target_namespace(source.namespace), source.workload_kind, source.workload_name) == (
        target.namespace,
        target.kind,
        target.name,
    )


@dataclass(frozen=True, slots=True)
class GroupVersionKind:
    group: str
    version: str
    kind: str

    @classmethod
    def from_api_version(cls, api_version: str, kind: str) -> GroupVersionKind:
        group, separator, version = api_version.rpartition("/")
        return cls(group=group if separator else "", version=version, kind=kind)

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version


@dataclass(frozen=True, slots=True)
class ManagedResource:
    """One inventory entry of one controller."""

    gvk: GroupVersionKind
    namespace: str
    name: str
    controller: ControllerIdentity

    @property
    def ref(self) -> ResourceTarget:
        return ResourceTarget(
            api_version=self.gvk.api_version, kind=self.gvk.kind, namespace=self.namespace, name=self.name
        )


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    """Which controller manages a resource, and where its Git source lives when known."""

    ref: ResourceTarget
    status: Literal["resolved", "ambiguous"]
    controllers: tuple[ControllerIdentity, ...]
    location: WorkloadSource | None = None
    reason: str | None = None
    #: Whether the managing controller prunes removed resources (``None`` when unknown).
    prune: bool | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.location is not None

    def workload_source(self) -> WorkloadSource | None:
        """The controller's resolved source addressed at this resource (namespaced resources only).

        Planners take a ``WorkloadSource`` whose identity matches the action target; the
        Git location, renderer and controller evidence are the controller's own.
        """
        if not self.resolved or self.location is None:
            return None
        evidence = {
            **self.location.evidence,
            CONTROLLER_PRUNE_EVIDENCE: self.prune,
            RESOURCE_BINDING_EVIDENCE: True,
        }
        if source_matches_target(self.location, self.ref):
            return self.location.model_copy(update={"evidence": evidence})
        identity = "\0".join((self.location.id, self.ref.api_version, self.ref.kind, self.ref.namespace, self.ref.name))
        return self.location.model_copy(
            update={
                "id": f"{self.location.controller_kind.value}-{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
                "namespace": source_namespace(self.ref.namespace),
                "workload_kind": self.ref.kind,
                "workload_name": self.ref.name,
                "evidence": evidence,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        """JSON form persisted in ``resource_bindings`` (migration 0087)."""
        return {
            "ref": self.ref.model_dump(mode="json"),
            "status": self.status,
            "controllers": [[kind.value, namespace, name] for kind, namespace, name in self.controllers],
            "location": self.location.model_dump(mode="json") if self.location is not None else None,
            "reason": self.reason,
            "prune": self.prune,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ResourceBinding:
        location = payload.get("location")
        status = payload.get("status")
        if status not in ("resolved", "ambiguous"):
            raise ValueError("resource binding status must be resolved or ambiguous")
        prune = payload.get("prune")
        return cls(
            ref=ResourceTarget.model_validate(payload["ref"]),
            status=status,
            controllers=tuple(
                (ControllerKind(str(item[0])), str(item[1]), str(item[2])) for item in payload.get("controllers") or ()
            ),
            location=WorkloadSource.model_validate(location) if isinstance(location, Mapping) else None,
            reason=str(payload["reason"]) if payload.get("reason") is not None else None,
            prune=prune if isinstance(prune, bool) else None,
        )


#: Cluster-scoped kinds by (group, kind). Argo CD lists them without a namespace; any other
#: namespace-less entry is namespaced in the destination namespace. A cluster-scoped kind
#: missing here is recorded under that namespace, so a cluster-scoped lookup finds it
#: unmanaged and fails closed.
CLUSTER_SCOPED_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("", "Namespace"),
        ("", "Node"),
        ("", "PersistentVolume"),
        ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
        ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
        ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
        ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
        ("apiextensions.k8s.io", "CustomResourceDefinition"),
        ("apiregistration.k8s.io", "APIService"),
        ("cert-manager.io", "ClusterIssuer"),
        ("certificates.k8s.io", "CertificateSigningRequest"),
        ("flowcontrol.apiserver.k8s.io", "FlowSchema"),
        ("flowcontrol.apiserver.k8s.io", "PriorityLevelConfiguration"),
        ("karpenter.k8s.aws", "EC2NodeClass"),
        ("karpenter.sh", "NodePool"),
        ("networking.k8s.io", "IngressClass"),
        ("node.k8s.io", "RuntimeClass"),
        ("rbac.authorization.k8s.io", "ClusterRole"),
        ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
        ("scheduling.k8s.io", "PriorityClass"),
        ("snapshot.storage.k8s.io", "VolumeSnapshotClass"),
        ("storage.k8s.io", "CSIDriver"),
        ("storage.k8s.io", "CSINode"),
        ("storage.k8s.io", "StorageClass"),
        ("storage.k8s.io", "VolumeAttachment"),
    }
)


def argo_application_inventory(application: Mapping[str, Any]) -> list[ManagedResource]:
    """Every resource an Argo CD Application lists in ``status.resources``."""
    metadata = as_mapping(application.get("metadata")) or {}
    status = as_mapping(application.get("status")) or {}
    name = text(metadata.get("name"))
    namespace = text(metadata.get("namespace"))
    raw_resources = status.get("resources")
    if name is None or namespace is None or not isinstance(raw_resources, list):
        return []
    spec = as_mapping(application.get("spec")) or {}
    destination_namespace = text((as_mapping(spec.get("destination")) or {}).get("namespace")) or ""
    controller: ControllerIdentity = (ControllerKind.ARGOCD, namespace, name)
    resources: list[ManagedResource] = []
    for raw in raw_resources:
        resource = as_mapping(raw)
        if resource is None:
            continue
        kind = text(resource.get("kind"))
        resource_name = text(resource.get("name"))
        version = text(resource.get("version"))
        if kind is None or resource_name is None or version is None:
            continue
        group = text(resource.get("group")) or ""
        resource_namespace = text(resource.get("namespace"))
        if resource_namespace is None and (group, kind) not in CLUSTER_SCOPED_KINDS:
            # Like workload discovery: a namespaced resource Argo CD lists without a
            # namespace lives in the Application's destination namespace.
            resource_namespace = destination_namespace
        resources.append(
            ManagedResource(
                gvk=GroupVersionKind(group=group, version=version, kind=kind),
                namespace=resource_namespace or "",
                name=resource_name,
                controller=controller,
            )
        )
    return _unique(resources)


def argo_application_prunes(application: Mapping[str, Any]) -> bool | None:
    """Whether an Argo CD Application prunes resources removed from Git.

    ``True`` with ``syncPolicy.automated.prune: true``; ``False`` with automated sync
    without prune; ``None`` for manual sync (a person decides when syncing).
    """
    spec = as_mapping(application.get("spec")) or {}
    policy = as_mapping(spec.get("syncPolicy")) or {}
    automated = as_mapping(policy.get("automated"))
    if automated is None:
        return None
    return automated.get("prune") is True


def flux_kustomization_prunes(kustomization: Mapping[str, Any]) -> bool:
    """Whether a Flux Kustomization garbage-collects resources removed from Git (``spec.prune``)."""
    spec = as_mapping(kustomization.get("spec")) or {}
    return spec.get("prune") is True


def flux_kustomization_inventory(kustomization: Mapping[str, Any]) -> list[ManagedResource]:
    """Every resource a Flux Kustomization lists in ``status.inventory.entries``."""
    metadata = as_mapping(kustomization.get("metadata")) or {}
    status = as_mapping(kustomization.get("status")) or {}
    inventory = as_mapping(status.get("inventory")) or {}
    name = text(metadata.get("name"))
    namespace = text(metadata.get("namespace"))
    entries = inventory.get("entries")
    if name is None or namespace is None or not isinstance(entries, list):
        return []
    controller: ControllerIdentity = (ControllerKind.FLUX_KUSTOMIZATION, namespace, name)
    resources: list[ManagedResource] = []
    for entry in entries:
        entry_mapping = as_mapping(entry)
        inventory_id = text(entry_mapping.get("id")) if entry_mapping is not None else None
        if entry_mapping is None or inventory_id is None:
            continue
        target = _parse_inventory_id(inventory_id, text(entry_mapping.get("v")))
        if target is None:
            continue
        resources.append(
            ManagedResource(
                gvk=GroupVersionKind.from_api_version(target.api_version, target.kind),
                namespace=target.namespace,
                name=target.name,
                controller=controller,
            )
        )
    return _unique(resources)


@dataclass
class ResourceBindingIndex:
    """Inventories of every controller, looked up by group, kind, namespace and name."""

    _resources: dict[tuple[str, str, str, str], list[ManagedResource]] = field(default_factory=dict)
    _locations: dict[ControllerIdentity, WorkloadSource] = field(default_factory=dict)
    _prune: dict[ControllerIdentity, bool | None] = field(default_factory=dict)
    #: Controllers that deploy to another cluster (never resolve a local resource).
    _remote: set[ControllerIdentity] = field(default_factory=set)

    def add_inventory(self, resources: Iterable[ManagedResource]) -> None:
        for resource in resources:
            key = (resource.gvk.group, resource.gvk.kind, resource.namespace, resource.name)
            entries = self._resources.setdefault(key, [])
            if all(entry.controller != resource.controller for entry in entries):
                entries.append(resource)

    def add_argo_application(self, application: Mapping[str, Any], local_cluster_aliases: Iterable[str] = ()) -> None:
        resources = argo_application_inventory(application)
        self.add_inventory(resources)
        spec = as_mapping(application.get("spec")) or {}
        remote = not argo_destination_is_local(as_mapping(spec.get("destination")), local_cluster_aliases)
        for controller in {resource.controller for resource in resources}:
            self._prune[controller] = argo_application_prunes(application)
            if remote:
                self._remote.add(controller)

    def add_flux_kustomization(self, kustomization: Mapping[str, Any]) -> None:
        resources = flux_kustomization_inventory(kustomization)
        self.add_inventory(resources)
        remote = flux_targets_remote_cluster(as_mapping(kustomization.get("spec")))
        for controller in {resource.controller for resource in resources}:
            self._prune[controller] = flux_kustomization_prunes(kustomization)
            if remote:
                self._remote.add(controller)

    def managed(self, kinds: Iterable[tuple[str, str]] | None = None) -> list[ManagedResource]:
        """Every inventory entry, optionally only of the ``(group, kind)`` pairs in ``kinds``."""
        selected = set(kinds) if kinds is not None else None
        return [
            entry
            for (group, kind, _namespace, _name), entries in sorted(self._resources.items())
            if selected is None or (group, kind) in selected
            for entry in entries
        ]

    def bindings(self, kinds: Iterable[tuple[str, str]] | None = None) -> list[ResourceBinding]:
        """The binding of every listed resource of ``kinds`` (one per resource)."""
        bindings: dict[tuple[str, str, str, str], ResourceBinding] = {}
        for entry in self.managed(kinds):
            key = (entry.gvk.group, entry.gvk.kind, entry.namespace, entry.name)
            if key not in bindings:
                binding = self.resolve(entry.ref)
                if binding is not None:
                    bindings[key] = binding
        return [bindings[key] for key in sorted(bindings)]

    def add_sources(self, sources: Iterable[WorkloadSource]) -> None:
        """Record each controller's resolved Git location from its discovered workload bindings.

        A controller whose resolved workload bindings disagree on the location (renderer,
        repository, ref or path) gets none, so its non-workload resources stay unresolved.
        """
        conflicting: set[ControllerIdentity] = set()
        for source in sorted(sources, key=lambda item: item.id):
            if source.status is not BindingStatus.RESOLVED:
                continue
            controller = (source.controller_kind, source.controller_namespace, source.controller_name)
            if controller in self._remote:
                continue
            existing = self._locations.get(controller)
            if existing is None:
                if controller not in conflicting:
                    self._locations[controller] = source
            elif _location(existing) != _location(source):
                conflicting.add(controller)
                del self._locations[controller]

    def resource_bindings(self, gvk: GroupVersionKind, namespace: str, name: str) -> list[ManagedResource]:
        """Every controller inventory entry for the resource (version-insensitive: one object, many versions)."""
        return list(self._resources.get((gvk.group, gvk.kind, namespace, name), ()))

    def resolve(self, ref: ResourceTarget) -> ResourceBinding | None:
        """The binding of ``ref``; ``None`` when no controller manages it."""
        gvk = GroupVersionKind.from_api_version(ref.api_version, ref.kind)
        entries = self.resource_bindings(gvk, ref.namespace, ref.name)
        if not entries:
            return None
        controllers = tuple(
            sorted({entry.controller for entry in entries}, key=lambda item: (item[0].value, *item[1:]))
        )
        if len(controllers) > 1:
            return ResourceBinding(
                ref=ref, status="ambiguous", controllers=controllers, reason="managed-by-multiple-controllers"
            )
        if controllers[0] in self._remote:
            return ResourceBinding(
                ref=ref,
                status="resolved",
                controllers=controllers,
                location=None,
                reason=DESTINATION_REMOTE_REASON,
                prune=self._prune.get(controllers[0]),
            )
        location = self._locations.get(controllers[0])
        return ResourceBinding(
            ref=ref,
            status="resolved",
            controllers=controllers,
            location=location,
            reason=None if location is not None else "controller-source-unresolved",
            prune=self._prune.get(controllers[0]),
        )


class UnboundResourceError(ValueError):
    """A resource an action needs is not managed by exactly one resolved GitOps controller."""


def bind_resources(index: ResourceBindingIndex, refs: Iterable[ResourceTarget]) -> list[ResourceBinding]:
    """Resolve every resource an action needs (``ActionKindSpec.targets``) or fail closed with the reason."""
    bindings: list[ResourceBinding] = []
    for ref in refs:
        described = f"{ref.kind} {ref.namespace + '/' if ref.namespace else ''}{ref.name}"
        binding = index.resolve(ref)
        if binding is None:
            raise UnboundResourceError(f"{described} is not managed by an Argo CD Application or Flux Kustomization")
        if binding.status == "ambiguous":
            raise UnboundResourceError(f"{described} is managed by several GitOps controllers")
        if binding.reason == DESTINATION_REMOTE_REASON:
            raise UnboundResourceError(
                f"{described} is listed by a GitOps controller that deploys to another cluster (destination_remote)"
            )
        if not binding.resolved:
            raise UnboundResourceError(f"the Git source of the controller managing {described} is unresolved")
        bindings.append(binding)
    return bindings


def _location(source: WorkloadSource) -> tuple[Any, ...]:
    return (source.provider_id, source.repo_url, source.ref, source.path, source.renderer)


def _parse_inventory_id(inventory_id: str, version: str | None) -> ResourceTarget | None:
    """Parse a Flux inventory ID, including cluster-scoped entries (empty namespace)."""
    try:
        return parse_flux_inventory_id(inventory_id, version=version)
    except ValueError:
        pass
    parts = inventory_id.split("_")
    if len(parts) != 4 or parts[0]:
        return None
    try:
        target = parse_flux_inventory_id("_".join(("cluster-scoped", *parts[1:])), version=version)
    except ValueError:
        return None
    return target.model_copy(update={"namespace": ""})


def _unique(resources: list[ManagedResource]) -> list[ManagedResource]:
    unique: dict[tuple[str, str, str, str], ManagedResource] = {}
    for resource in resources:
        unique.setdefault((resource.gvk.group, resource.gvk.kind, resource.namespace, resource.name), resource)
    return list(unique.values())
