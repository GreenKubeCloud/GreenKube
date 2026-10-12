"""Read-only Kubernetes discovery for Argo CD and Flux GitOps resources."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from greenkube.gitops.contracts import BindingStatus, ControllerKind, RendererKind, WorkloadSource
from greenkube.gitops.discovery.argo import discover_argo_application
from greenkube.gitops.discovery.common import (
    WORKLOAD_KINDS,
    as_mapping,
    text,
    workload_source,
)
from greenkube.gitops.discovery.flux import (
    discover_flux_helm_release,
    discover_flux_helm_release_manifest_source,
    discover_flux_helm_release_values_sources,
    discover_flux_kustomization,
)

_ARGO_APPLICATIONS = "argocd"
_FLUX_KUSTOMIZATIONS = ControllerKind.FLUX_KUSTOMIZATION.value
_FLUX_HELM_RELEASES = ControllerKind.FLUX_HELM_RELEASE.value


@dataclass(frozen=True)
class _ResourceList:
    capability: str
    group: str
    version: str
    plural: str


_APPLICATIONS = _ResourceList(_ARGO_APPLICATIONS, "argoproj.io", "v1alpha1", "applications")
_KUSTOMIZATIONS = _ResourceList(
    _FLUX_KUSTOMIZATIONS,
    "kustomize.toolkit.fluxcd.io",
    "v1",
    "kustomizations",
)
_HELM_RELEASES = _ResourceList(_FLUX_HELM_RELEASES, "helm.toolkit.fluxcd.io", "v2", "helmreleases")
_HELM_CHARTS = _ResourceList("flux_source_helm_chart", "source.toolkit.fluxcd.io", "v1", "helmcharts")
_FLUX_SOURCES = (
    _ResourceList("flux_source_git_repository", "source.toolkit.fluxcd.io", "v1", "gitrepositories"),
    _ResourceList("flux_source_oci_repository", "source.toolkit.fluxcd.io", "v1", "ocirepositories"),
    _ResourceList("flux_source_helm_repository", "source.toolkit.fluxcd.io", "v1", "helmrepositories"),
    _ResourceList("flux_source_bucket", "source.toolkit.fluxcd.io", "v1", "buckets"),
)
_RESOURCE_LISTS = (_APPLICATIONS, _KUSTOMIZATIONS, _HELM_RELEASES, *_FLUX_SOURCES, _HELM_CHARTS)
_DEFAULT_WORKLOAD_INVENTORY_PAGE_SIZE = 250
_MAX_WORKLOAD_INVENTORY_PAGE_SIZE = 500
_DEFAULT_WORKLOAD_INVENTORY_MAX_PAGES = 20
_MAX_WORKLOAD_INVENTORY_MAX_PAGES = 20
_WORKLOAD_LISTS = (
    ("Deployment", "list_deployment_for_all_namespaces"),
    ("StatefulSet", "list_stateful_set_for_all_namespaces"),
    ("DaemonSet", "list_daemon_set_for_all_namespaces"),
)
_MISSING_CRD_EXCEPTION_NAMES = frozenset({"NoMatch", "NoMatchError", "ResourceNotFoundError"})


class KubernetesWorkloadInventoryError(RuntimeError):
    """Raised when a complete, valid workload inventory cannot be read."""


class KubernetesWorkloadInventoryLimitError(KubernetesWorkloadInventoryError):
    """Raised when a workload inventory exceeds its configured pagination bound."""


class KubernetesWorkloadInventoryPaginationError(KubernetesWorkloadInventoryError):
    """Raised when Kubernetes returns invalid continuation metadata."""


@dataclass(frozen=True)
class CapabilityAvailability:
    """Availability of one controller or Flux source CRD capability."""

    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class KubernetesDiscoveryResult:
    """Workload candidates and per-CRD availability from one discovery pass."""

    candidates: list[WorkloadSource]
    availability: dict[str, CapabilityAvailability]


class KubernetesWorkloadInventoryReader:
    """Read a bounded, normalized inventory of live apps workloads."""

    def __init__(
        self,
        apps_v1_api: Any,
        *,
        autoscaling_v2_api: Any | None = None,
        policy_v1_api: Any | None = None,
        page_size: int = _DEFAULT_WORKLOAD_INVENTORY_PAGE_SIZE,
        max_pages: int = _DEFAULT_WORKLOAD_INVENTORY_MAX_PAGES,
    ) -> None:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= _MAX_WORKLOAD_INVENTORY_PAGE_SIZE
        ):
            raise ValueError(f"page_size must be an integer between 1 and {_MAX_WORKLOAD_INVENTORY_PAGE_SIZE}")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= _MAX_WORKLOAD_INVENTORY_MAX_PAGES
        ):
            raise ValueError(f"max_pages must be an integer between 1 and {_MAX_WORKLOAD_INVENTORY_MAX_PAGES}")
        self._apps_v1_api = apps_v1_api
        self._autoscaling_v2_api = autoscaling_v2_api
        self._policy_v1_api = policy_v1_api
        self.page_size = page_size
        self.max_pages = max_pages

    async def list_workloads(self) -> list[Mapping[str, Any]]:
        """List workloads and attach optional HPA/PDB policy evidence."""
        workloads: list[Mapping[str, Any]] = []
        for kind, method_name in _WORKLOAD_LISTS:
            workloads.extend(await self._list_kind(kind, method_name))
        if self._autoscaling_v2_api is not None or self._policy_v1_api is not None:
            snapshot = await _read_policy_snapshot(self._autoscaling_v2_api, self._policy_v1_api)
            workloads = [_with_policy_snapshot(workload, snapshot) for workload in workloads]
        return workloads

    async def _list_kind(self, kind: str, method_name: str) -> list[Mapping[str, Any]]:
        workloads: list[Mapping[str, Any]] = []
        continuation: str | None = None
        seen_continuations: set[str] = set()

        for page_index in range(self.max_pages):
            kwargs: dict[str, Any] = {"limit": self.page_size}
            if continuation is not None:
                kwargs["_continue"] = continuation
            response = await getattr(self._apps_v1_api, method_name)(**kwargs)
            items = _listed_objects(response, kind)
            if len(items) > self.page_size:
                raise KubernetesWorkloadInventoryLimitError(
                    f"Kubernetes returned more than {self.page_size} {kind} objects in one inventory page."
                )
            workloads.extend(_normalize_workload(item, kind) for item in items)

            continuation = _list_continue(response, kind)
            if continuation is None:
                return workloads
            if continuation in seen_continuations:
                raise KubernetesWorkloadInventoryPaginationError(
                    f"Kubernetes returned a repeated continuation token while listing {kind} workloads."
                )
            seen_continuations.add(continuation)
            if page_index == self.max_pages - 1:
                raise KubernetesWorkloadInventoryLimitError(
                    f"Kubernetes {kind} workload inventory exceeds the {self.max_pages}-page limit "
                    f"({self.page_size} objects per page)."
                )

        return workloads


class KubernetesDiscoverySource:
    """List GitOps custom resources and pass them to the pure discovery adapters.

    The custom-object API is injected so callers own Kubernetes configuration
    and client lifecycle. ``resources`` supplies live workload objects used by
    the Flux HelmRelease adapter; this source itself only lists GitOps CRDs.
    """

    def __init__(self, custom_objects_api: Any) -> None:
        self._custom_objects_api = custom_objects_api

    async def check_availability(
        self,
        *,
        namespace: str | None = None,
    ) -> dict[str, CapabilityAvailability]:
        """Probe controller CRD availability without materializing discovered objects."""
        availability: dict[str, CapabilityAvailability] = {}
        for resource_list in _RESOURCE_LISTS:
            try:
                await self._list(resource_list, namespace=namespace, limit=1)
            except Exception as error:
                if not _is_missing_crd(error):
                    raise
                availability[resource_list.capability] = CapabilityAvailability(
                    available=False,
                    reason="crd-not-found",
                )
            else:
                availability[resource_list.capability] = CapabilityAvailability(available=True)
        return availability

    async def discover(
        self,
        *,
        cluster: str,
        provider_id: str,
        namespace: str | None = None,
        resources: Iterable[Mapping[str, Any]] = (),
    ) -> KubernetesDiscoveryResult:
        """List configured CRDs cluster-wide or in one namespace and adapt them."""
        items_by_capability: dict[str, list[Mapping[str, Any]]] = {}
        availability: dict[str, CapabilityAvailability] = {}

        for resource_list in _RESOURCE_LISTS:
            try:
                response = await self._list(resource_list, namespace=namespace)
            except Exception as error:
                if not _is_missing_crd(error):
                    raise
                items_by_capability[resource_list.capability] = []
                availability[resource_list.capability] = CapabilityAvailability(
                    available=False,
                    reason="crd-not-found",
                )
            else:
                items_by_capability[resource_list.capability] = _items(response)
                availability[resource_list.capability] = CapabilityAvailability(available=True)

        source_objects = [
            source for resource_list in _FLUX_SOURCES for source in items_by_capability[resource_list.capability]
        ]
        source_objects.extend(items_by_capability[_HELM_CHARTS.capability])
        live_resources = [resource for resource in resources if isinstance(resource, Mapping)]
        candidates: list[WorkloadSource] = []

        for application in items_by_capability[_APPLICATIONS.capability]:
            try:
                candidates.extend(
                    discover_argo_application(
                        application,
                        cluster=cluster,
                        provider_id=provider_id,
                        live_resources=live_resources,
                    )
                )
            except Exception:
                candidates.extend(
                    _blocked_argo_application_candidates(
                        application,
                        cluster=cluster,
                        provider_id=provider_id,
                    )
                )

        for kustomization in items_by_capability[_KUSTOMIZATIONS.capability]:
            candidates.extend(
                discover_flux_kustomization(
                    kustomization,
                    source_objects,
                    cluster=cluster,
                    provider_id=provider_id,
                )
            )

        for helm_release in items_by_capability[_HELM_RELEASES.capability]:
            manifest_source = discover_flux_helm_release_manifest_source(
                helm_release,
                items_by_capability[_KUSTOMIZATIONS.capability],
                source_objects,
                provider_id=provider_id,
            )
            git_managed_values = discover_flux_helm_release_values_sources(
                helm_release,
                items_by_capability[_KUSTOMIZATIONS.capability],
                source_objects,
                provider_id=provider_id,
            )
            candidates.extend(
                discover_flux_helm_release(
                    helm_release,
                    live_resources,
                    source_objects,
                    cluster=cluster,
                    provider_id=provider_id,
                    manifest_source=manifest_source,
                    git_managed_values=git_managed_values,
                )
            )

        candidates = [_with_live_workload_evidence(candidate, live_resources) for candidate in candidates]
        return KubernetesDiscoveryResult(candidates=candidates, availability=availability)

    async def _list(
        self,
        resource: _ResourceList,
        *,
        namespace: str | None,
        limit: int | None = None,
    ) -> Any:
        options = {"limit": limit} if limit is not None else {}
        if namespace is None:
            return await self._custom_objects_api.list_cluster_custom_object(
                group=resource.group,
                version=resource.version,
                plural=resource.plural,
                **options,
            )
        return await self._custom_objects_api.list_namespaced_custom_object(
            group=resource.group,
            version=resource.version,
            namespace=namespace,
            plural=resource.plural,
            **options,
        )


def _items(response: Any) -> list[Mapping[str, Any]]:
    if isinstance(response, Mapping):
        raw_items = response.get("items")
    else:
        raw_items = getattr(response, "items", None)
    if not isinstance(raw_items, (list, tuple)):
        return []
    return [item for item in raw_items if isinstance(item, Mapping)]


def _blocked_argo_application_candidates(
    application: Mapping[str, Any],
    *,
    cluster: str,
    provider_id: str,
) -> list[WorkloadSource]:
    metadata = as_mapping(application.get("metadata")) or {}
    spec = as_mapping(application.get("spec")) or {}
    status = as_mapping(application.get("status")) or {}
    destination = as_mapping(spec.get("destination")) or {}
    application_name = text(metadata.get("name")) or "unknown-application"
    application_namespace = text(metadata.get("namespace")) or "default"
    destination_namespace = text(destination.get("namespace"))
    raw_resources = status.get("resources")
    if not isinstance(raw_resources, list):
        return []

    candidates: list[WorkloadSource] = []
    for raw_resource in raw_resources:
        resource = as_mapping(raw_resource)
        if resource is None:
            continue
        kind = text(resource.get("kind"))
        name = text(resource.get("name"))
        namespace = text(resource.get("namespace")) or destination_namespace
        if kind is None or kind not in WORKLOAD_KINDS or name is None or namespace is None:
            continue
        candidates.append(
            workload_source(
                cluster=cluster,
                workload_namespace=namespace,
                workload_kind=kind,
                workload_name=name,
                provider_id=text(provider_id) or "unresolved",
                repo_url="unresolved",
                ref="HEAD",
                revision=None,
                path=".",
                controller_kind=ControllerKind.ARGOCD,
                controller_name=application_name,
                controller_namespace=application_namespace,
                controller_uid=text(metadata.get("uid")),
                source_revision=None,
                renderer=RendererKind.RAW,
                renderer_config={},
                status=BindingStatus.BLOCKED,
                evidence={
                    "application": {"name": application_name, "namespace": application_namespace},
                    "resource": {
                        "kind": kind,
                        "namespace": namespace,
                        "name": name,
                    },
                    "discovery_error": "adapter-error",
                },
                reason="discovery-adapter-error",
                identity_salt="adapter-error",
            )
        )
    return candidates


def _listed_objects(response: Any, kind: str) -> list[Any]:
    raw_items = _field(response, "items")
    if not isinstance(raw_items, (list, tuple)):
        raise KubernetesWorkloadInventoryError(f"Kubernetes returned an invalid {kind} workload list response.")
    return list(raw_items)


def _list_continue(response: Any, kind: str) -> str | None:
    metadata = _field(response, "metadata")
    token = _field(metadata, "_continue")
    if token is None:
        token = _field(metadata, "continue")
    if token is None or token == "":
        return None
    if not isinstance(token, str):
        raise KubernetesWorkloadInventoryPaginationError(
            f"Kubernetes returned an invalid continuation token while listing {kind} workloads."
        )
    return token


def _normalize_workload(resource: Any, kind: str) -> Mapping[str, Any]:
    metadata = _field(resource, "metadata")
    normalized_metadata: dict[str, Any] = {
        "labels": _string_mapping(_field(metadata, "labels")),
        "annotations": _string_mapping(_field(metadata, "annotations")),
    }
    for name, aliases in (
        ("name", ("name",)),
        ("namespace", ("namespace",)),
        ("uid", ("uid",)),
        ("generation", ("generation",)),
        ("deletionTimestamp", ("deletionTimestamp", "deletion_timestamp")),
    ):
        value = _field_any(metadata, *aliases)
        if value is not None and not isinstance(value, (dict, list, tuple)):
            normalized_metadata[name] = value

    api_version = _field(resource, "apiVersion")
    if not isinstance(api_version, str) or not api_version:
        api_version = _field(resource, "api_version")
    if not isinstance(api_version, str) or not api_version:
        api_version = "apps/v1"

    normalized: dict[str, Any] = {"apiVersion": api_version, "kind": kind, "metadata": normalized_metadata}
    spec = as_mapping(_field(resource, "spec"))
    if spec is None:
        spec = _object_mapping(_field(resource, "spec"))
    if spec is not None:
        normalized["spec"] = _normalized_spec(spec)
    status = as_mapping(_field(resource, "status"))
    if status is None:
        status = _object_mapping(_field(resource, "status"))
    if status is not None:
        normalized["status"] = _normalized_status(status)
    return normalized


def _field_any(value: Any, *names: str) -> Any:
    for name in names:
        found = _field(value, name)
        if found is not None:
            return found
    return None


def _object_mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict()
        except Exception:
            return None
        return converted if isinstance(converted, Mapping) else None
    return None


def _normalized_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, aliases in (
        ("replicas", ("replicas",)),
        ("suspend", ("suspend",)),
        ("minReadySeconds", ("minReadySeconds", "min_ready_seconds")),
        ("updateStrategy", ("updateStrategy", "update_strategy")),
    ):
        value = _field_any(spec, *aliases)
        if value is not None and not isinstance(value, (dict, list, tuple)):
            normalized[key] = value
    template = as_mapping(_field(spec, "template")) or _object_mapping(_field(spec, "template"))
    if template is not None:
        template_metadata = as_mapping(_field(template, "metadata")) or _object_mapping(_field(template, "metadata"))
        if template_metadata is not None:
            normalized["template"] = {
                "metadata": {
                    "labels": _string_mapping(_field(template_metadata, "labels")),
                    "annotations": _string_mapping(_field(template_metadata, "annotations")),
                }
            }
    return normalized


def _normalized_status(status: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, aliases in (
        ("observedGeneration", ("observedGeneration", "observed_generation")),
        ("replicas", ("replicas",)),
        ("updatedReplicas", ("updatedReplicas", "updated_replicas")),
        ("readyReplicas", ("readyReplicas", "ready_replicas")),
        ("availableReplicas", ("availableReplicas", "available_replicas")),
        ("unavailableReplicas", ("unavailableReplicas", "unavailable_replicas")),
        ("currentReplicas", ("currentReplicas", "current_replicas")),
        ("numberReady", ("numberReady", "number_ready")),
        ("numberAvailable", ("numberAvailable", "number_available")),
        ("numberUnavailable", ("numberUnavailable", "number_unavailable")),
        ("updatedNumberScheduled", ("updatedNumberScheduled", "updated_number_scheduled")),
        ("desiredNumberScheduled", ("desiredNumberScheduled", "desired_number_scheduled")),
        ("currentRevision", ("currentRevision", "current_revision")),
        ("updateRevision", ("updateRevision", "update_revision")),
    ):
        value = _field_any(status, *aliases)
        if value is not None and not isinstance(value, (dict, list, tuple)):
            normalized[key] = value
    conditions = _field(status, "conditions")
    if isinstance(conditions, (list, tuple)):
        normalized["conditions"] = [dict(condition) for condition in conditions if isinstance(condition, Mapping)]
    return normalized


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}


def _is_missing_crd(error: Exception) -> bool:
    status = getattr(error, "status", None)
    if status == 404 or status == "404":
        return True
    reason = getattr(error, "reason", None)
    if isinstance(reason, str) and reason in _MISSING_CRD_EXCEPTION_NAMES:
        return True
    return any(base.__name__ in _MISSING_CRD_EXCEPTION_NAMES for base in type(error).__mro__)


@dataclass(frozen=True, slots=True)
class _PolicySnapshot:
    checked: bool
    hpa_targets: Mapping[tuple[str, str, str], Mapping[str, Any]]
    pod_disruption_budgets: tuple[Mapping[str, Any], ...]
    reason: str | None = None


async def _read_policy_snapshot(
    autoscaling_v2_api: Any | None,
    policy_v1_api: Any | None,
) -> _PolicySnapshot:
    hpa_targets: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    pdbs: list[Mapping[str, Any]] = []
    errors: list[str] = []

    if autoscaling_v2_api is not None:
        try:
            response = await autoscaling_v2_api.list_horizontal_pod_autoscaler_for_all_namespaces()
            for item in _listed_objects(response, "HorizontalPodAutoscaler"):
                normalized = _normalize_hpa(item)
                if normalized is not None:
                    hpa_targets[normalized[0]] = normalized[1]
        except Exception:
            errors.append("hpa-read-failed")

    if policy_v1_api is not None:
        try:
            response = await policy_v1_api.list_pod_disruption_budget_for_all_namespaces()
            for item in _listed_objects(response, "PodDisruptionBudget"):
                normalized = _normalize_pdb(item)
                if normalized is not None:
                    pdbs.append(normalized)
        except Exception:
            errors.append("pdb-read-failed")

    checked = autoscaling_v2_api is not None and policy_v1_api is not None and not errors
    return _PolicySnapshot(
        checked=checked,
        hpa_targets=hpa_targets,
        pod_disruption_budgets=tuple(pdbs),
        reason=",".join(errors) if errors else None,
    )


def _normalize_hpa(resource: Any) -> tuple[tuple[str, str, str], Mapping[str, Any]] | None:
    metadata = _field(resource, "metadata")
    spec = _field(resource, "spec")
    namespace = _field(metadata, "namespace")
    name = _field(metadata, "name")
    target = _field(spec, "scaleTargetRef") or _field(spec, "scale_target_ref")
    kind = _field(target, "kind")
    target_name = _field(target, "name")
    if not all(isinstance(value, str) and value for value in (namespace, name, kind, target_name)):
        return None
    metrics = _field(spec, "metrics")
    metric_types: list[str] = []
    if isinstance(metrics, (list, tuple)):
        for metric in metrics:
            metric_type = _field(metric, "type")
            if isinstance(metric_type, str) and metric_type:
                metric_types.append(metric_type)
    return (namespace, kind, target_name), {
        "name": name,
        "namespace": namespace,
        "target_kind": kind,
        "target_name": target_name,
        "metric_types": sorted(set(metric_types)),
        "min_replicas": _field(spec, "minReplicas") if isinstance(_field(spec, "minReplicas"), int) else None,
        "max_replicas": _field(spec, "maxReplicas") if isinstance(_field(spec, "maxReplicas"), int) else None,
    }


def _normalize_pdb(resource: Any) -> Mapping[str, Any] | None:
    metadata = _field(resource, "metadata")
    spec = _field(resource, "spec")
    namespace = _field(metadata, "namespace")
    name = _field(metadata, "name")
    if not isinstance(namespace, str) or not namespace or not isinstance(name, str) or not name:
        return None
    selector = as_mapping(_field(spec, "selector")) or _object_mapping(_field(spec, "selector"))
    min_available = _field(spec, "minAvailable")
    if min_available is None:
        min_available = _field(spec, "min_available")
    max_unavailable = _field(spec, "maxUnavailable")
    if max_unavailable is None:
        max_unavailable = _field(spec, "max_unavailable")
    return {
        "name": name,
        "namespace": namespace,
        "selector": _normalized_selector(selector) if selector is not None else {},
        "min_available": (
            min_available if isinstance(min_available, (int, str)) and not isinstance(min_available, bool) else None
        ),
        "max_unavailable": max_unavailable
        if isinstance(max_unavailable, (int, str)) and not isinstance(max_unavailable, bool)
        else None,
    }


def _normalized_selector(selector: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    match_labels = as_mapping(_field(selector, "matchLabels")) or _object_mapping(_field(selector, "matchLabels"))
    if match_labels is not None:
        result["match_labels"] = _string_mapping(match_labels)
    expressions = _field(selector, "matchExpressions") or _field(selector, "match_expressions")
    if isinstance(expressions, (list, tuple)):
        normalized_expressions: list[dict[str, Any]] = []
        for expression in expressions:
            key = _field(expression, "key")
            operator = _field(expression, "operator")
            values = _field(expression, "values")
            if isinstance(key, str) and isinstance(operator, str) and isinstance(values, (list, tuple)):
                normalized_expressions.append(
                    {
                        "key": key,
                        "operator": operator,
                        "values": [value for value in values if isinstance(value, str)],
                    }
                )
        result["match_expressions"] = normalized_expressions
    return result


def _with_policy_snapshot(workload: Mapping[str, Any], snapshot: _PolicySnapshot) -> Mapping[str, Any]:
    result = dict(workload)
    result["_greenkube_policy_evidence"] = {
        "checked": snapshot.checked,
        "reason": snapshot.reason,
        "hpa_targets": [
            dict(hpa)
            for hpa in snapshot.hpa_targets.values()
            if hpa.get("namespace") == (workload.get("metadata") or {}).get("namespace")
            and hpa.get("target_kind") == workload.get("kind")
            and hpa.get("target_name") == (workload.get("metadata") or {}).get("name")
        ],
        "pod_disruption_budgets": [
            dict(pdb)
            for pdb in snapshot.pod_disruption_budgets
            if pdb.get("namespace") == workload.get("metadata", {}).get("namespace")
        ],
    }
    return result


def _with_live_workload_evidence(
    candidate: WorkloadSource,
    resources: Iterable[Mapping[str, Any]],
) -> WorkloadSource:
    resource = next(
        (
            item
            for item in resources
            if isinstance(item, Mapping)
            and text(_field(_field(item, "metadata"), "namespace")) == candidate.namespace
            and text(_field(item, "kind")) == candidate.workload_kind
            and text(_field(_field(item, "metadata"), "name")) == candidate.workload_name
        ),
        None,
    )
    if resource is None:
        return candidate

    metadata = _field(resource, "metadata") or {}
    normalized_metadata = as_mapping(metadata) or _object_mapping(metadata) or {}
    labels = _string_mapping(_field(normalized_metadata, "labels"))
    template = _field(_field(resource, "spec"), "template")
    template_metadata = _field(template, "metadata") if template is not None else None
    template_labels = _string_mapping(_field(template_metadata, "labels"))
    merged_labels = {**template_labels, **labels}
    rollout_in_progress = _workload_rollout_in_progress(resource, candidate.workload_kind)
    policy = _greenkube_policy_evidence(resource)
    hpa_pdb = _hpa_pdb_interaction(candidate, merged_labels, resource, policy)

    evidence = dict(candidate.evidence)
    evidence["workload_labels"] = merged_labels
    evidence["workload"] = {
        "labels": merged_labels,
        "annotations": _string_mapping(_field(normalized_metadata, "annotations")),
    }
    if rollout_in_progress is not None:
        evidence["rollout_in_progress"] = rollout_in_progress
    if hpa_pdb is not None:
        interactions_checked, conflict = hpa_pdb
        evidence["hpa_pdb_interactions_checked"] = interactions_checked
        if conflict is not None:
            evidence["hpa_pdb_conflict"] = conflict
    return candidate.model_copy(update={"evidence": evidence})


def _workload_rollout_in_progress(resource: Mapping[str, Any], kind: str) -> bool | None:
    spec = as_mapping(_field(resource, "spec")) or {}
    status = as_mapping(_field(resource, "status")) or {}
    generation = _field(_field(resource, "metadata") or {}, "generation")
    observed_generation = _field_any(status, "observedGeneration", "observed_generation")
    if isinstance(generation, int) and isinstance(observed_generation, int) and observed_generation < generation:
        return True

    desired = _field(spec, "replicas")
    desired = desired if isinstance(desired, int) and not isinstance(desired, bool) else 1
    updated = _field_any(status, "updatedReplicas", "updated_replicas")
    ready = _field_any(status, "readyReplicas", "ready_replicas")
    unavailable = _field_any(status, "unavailableReplicas", "unavailable_replicas")
    current = _field_any(status, "currentReplicas", "current_replicas")

    if kind == "Deployment":
        if isinstance(updated, int) and updated < desired:
            return True
        if isinstance(unavailable, int) and unavailable > 0:
            return True
        if isinstance(current, int) and isinstance(updated, int) and current > updated:
            return True
        if isinstance(ready, int) and ready < desired:
            return True
        return False
    if kind == "StatefulSet":
        update_revision = _field_any(status, "updateRevision", "update_revision")
        current_revision = _field_any(status, "currentRevision", "current_revision")
        if (
            isinstance(update_revision, str)
            and isinstance(current_revision, str)
            and update_revision != current_revision
        ):
            return True
        if isinstance(updated, int) and updated < desired:
            return True
        if isinstance(ready, int) and ready < desired:
            return True
        return False
    if kind == "DaemonSet":
        desired_scheduled = _field_any(status, "desiredNumberScheduled", "desired_number_scheduled")
        updated_scheduled = _field_any(status, "updatedNumberScheduled", "updated_number_scheduled")
        number_unavailable = _field_any(status, "numberUnavailable", "number_unavailable")
        number_ready = _field_any(status, "numberReady", "number_ready")
        if isinstance(desired_scheduled, int):
            if isinstance(updated_scheduled, int) and updated_scheduled < desired_scheduled:
                return True
            if isinstance(number_ready, int) and number_ready < desired_scheduled:
                return True
        if isinstance(number_unavailable, int) and number_unavailable > 0:
            return True
        return False
    return None


def _greenkube_policy_evidence(resource: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = _field(resource, "_greenkube_policy_evidence")
    return value if isinstance(value, Mapping) else None


def _hpa_pdb_interaction(
    candidate: WorkloadSource,
    labels: Mapping[str, str],
    resource: Mapping[str, Any],
    policy: Mapping[str, Any] | None,
) -> tuple[bool, bool | None] | None:
    if policy is None or policy.get("checked") is not True:
        return None
    hpa_present = any(
        isinstance(hpa, Mapping)
        and hpa.get("name")
        and text(hpa.get("target_kind")) == candidate.workload_kind
        and text(hpa.get("target_name")) == candidate.workload_name
        for hpa in policy.get("hpa_targets") or ()
    )
    desired = _field(as_mapping(_field(resource, "spec")) or {}, "replicas")
    desired = desired if isinstance(desired, int) and not isinstance(desired, bool) else None
    hard_pdb = any(
        _pdb_blocks_disruption(pdb, labels, desired_replicas=desired)
        for pdb in policy.get("pod_disruption_budgets") or ()
        if isinstance(pdb, Mapping)
    )
    return True, bool(hpa_present or hard_pdb)


def _pdb_blocks_disruption(
    pdb: Mapping[str, Any],
    labels: Mapping[str, str],
    *,
    desired_replicas: int | None,
) -> bool:
    selector = pdb.get("selector")
    if not isinstance(selector, Mapping) or not _selector_matches(selector, labels):
        return False
    max_unavailable = pdb.get("max_unavailable")
    if max_unavailable == 0:
        return True
    min_available = pdb.get("min_available")
    if min_available == "100%":
        return True
    if isinstance(min_available, int) and desired_replicas is not None and min_available >= desired_replicas:
        return True
    return False


def _selector_matches(selector: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    match_labels = selector.get("match_labels")
    if isinstance(match_labels, Mapping):
        if any(labels.get(key) != value for key, value in match_labels.items()):
            return False
    expressions = selector.get("match_expressions")
    if isinstance(expressions, (list, tuple)):
        for expression in expressions:
            if not isinstance(expression, Mapping):
                continue
            key = expression.get("key")
            operator = expression.get("operator")
            raw_values = expression.get("values")
            values = (
                [value for value in raw_values if isinstance(value, str)]
                if isinstance(raw_values, (list, tuple))
                else []
            )
            if not isinstance(key, str) or not isinstance(operator, str):
                continue
            if operator == "In" and labels.get(key) not in values:
                return False
            if operator == "NotIn" and labels.get(key) in values:
                return False
            if operator == "Exists" and key not in labels:
                return False
            if operator == "DoesNotExist" and key in labels:
                return False
    return True
