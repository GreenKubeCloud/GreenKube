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
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from greenkube.gitops.contracts import BindingStatus, ControllerKind, ResourceTarget, WorkloadSource
from greenkube.gitops.discovery.common import as_mapping, text
from greenkube.gitops.discovery.flux import parse_flux_inventory_id

ControllerIdentity = tuple[ControllerKind, str, str]


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

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.location is not None

    def workload_source(self) -> WorkloadSource | None:
        """The controller's resolved source addressed at this resource (namespaced resources only).

        Planners take a ``WorkloadSource`` whose identity matches the action target; the
        Git location, renderer and controller evidence are the controller's own.
        """
        if not self.resolved or self.location is None or not self.ref.namespace:
            return None
        if (self.ref.namespace, self.ref.kind, self.ref.name) == (
            self.location.namespace,
            self.location.workload_kind,
            self.location.workload_name,
        ):
            return self.location
        identity = "\0".join((self.location.id, self.ref.api_version, self.ref.kind, self.ref.namespace, self.ref.name))
        return self.location.model_copy(
            update={
                "id": f"{self.location.controller_kind.value}-{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
                "namespace": self.ref.namespace,
                "workload_kind": self.ref.kind,
                "workload_name": self.ref.name,
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
        resources.append(
            ManagedResource(
                gvk=GroupVersionKind(group=text(resource.get("group")) or "", version=version, kind=kind),
                namespace=text(resource.get("namespace")) or "",
                name=resource_name,
                controller=controller,
            )
        )
    return _unique(resources)


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

    def add_inventory(self, resources: Iterable[ManagedResource]) -> None:
        for resource in resources:
            key = (resource.gvk.group, resource.gvk.kind, resource.namespace, resource.name)
            entries = self._resources.setdefault(key, [])
            if all(entry.controller != resource.controller for entry in entries):
                entries.append(resource)

    def add_argo_application(self, application: Mapping[str, Any]) -> None:
        self.add_inventory(argo_application_inventory(application))

    def add_flux_kustomization(self, kustomization: Mapping[str, Any]) -> None:
        self.add_inventory(flux_kustomization_inventory(kustomization))

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
        location = self._locations.get(controllers[0])
        return ResourceBinding(
            ref=ref,
            status="resolved",
            controllers=controllers,
            location=location,
            reason=None if location is not None else "controller-source-unresolved",
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
