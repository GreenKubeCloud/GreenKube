"""Resource bindings by GVK and name from Argo CD and Flux inventories (H0d)."""

from __future__ import annotations

from typing import Any

import pytest

from greenkube.gitops.contracts import BindingStatus, ControllerKind, ResourceTarget
from greenkube.gitops.discovery.inventory import (
    CLUSTER_SCOPE_NAMESPACE,
    CONTROLLER_PRUNE_EVIDENCE,
    GroupVersionKind,
    ResourceBinding,
    ResourceBindingIndex,
    UnboundResourceError,
    argo_application_inventory,
    bind_resources,
    flux_kustomization_inventory,
    source_matches_target,
    target_namespace,
)
from tests.gitops.planning.test_raw_manifest import _source  # pyrefly: ignore[missing-import]

APPLICATION: dict[str, Any] = {
    "metadata": {"name": "payments-prod", "namespace": "argocd"},
    "spec": {"destination": {"namespace": "payments"}},
    "status": {
        "resources": [
            {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "payments", "name": "api"},
            {
                "group": "autoscaling",
                "version": "v2",
                "kind": "HorizontalPodAutoscaler",
                "namespace": "payments",
                "name": "api",
            },
            {"version": "v1", "kind": "PersistentVolume", "name": "pv-orphan"},
            {"kind": "Broken"},
        ]
    },
}
KUSTOMIZATION: dict[str, Any] = {
    "metadata": {"name": "platform", "namespace": "flux-system"},
    "status": {
        "inventory": {
            "entries": [
                {"id": "payments_api_autoscaling_HorizontalPodAutoscaler", "v": "v2"},
                {"id": "_default_karpenter.sh_NodePool", "v": "v1"},
                {"id": "not-an-id"},
            ]
        }
    },
}
HPA = ResourceTarget(api_version="autoscaling/v2", kind="HorizontalPodAutoscaler", namespace="payments", name="api")
PV = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-orphan")
NODEPOOL = ResourceTarget(api_version="karpenter.sh/v1", kind="NodePool", namespace="", name="default")


def test_argo_inventory_lists_every_kind_including_cluster_scoped_resources() -> None:
    refs = [resource.ref for resource in argo_application_inventory(APPLICATION)]

    assert refs == [
        ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        HPA,
        PV,
    ]


def test_argo_inventory_places_namespace_less_namespaced_resources_in_the_destination_namespace() -> None:
    application = {
        **APPLICATION,
        "status": {
            "resources": [
                {"group": "apps", "version": "v1", "kind": "Deployment", "name": "api"},
                {"group": "autoscaling", "version": "v2", "kind": "HorizontalPodAutoscaler", "name": "api"},
                {"version": "v1", "kind": "PersistentVolume", "name": "pv-orphan"},
                {"group": "karpenter.sh", "version": "v1", "kind": "NodePool", "name": "default"},
            ]
        },
    }

    refs = [resource.ref for resource in argo_application_inventory(application)]

    assert refs == [
        ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        HPA,
        PV,
        NODEPOOL,
    ]


def test_flux_inventory_parses_namespaced_and_cluster_scoped_ids() -> None:
    resources = flux_kustomization_inventory(KUSTOMIZATION)

    assert [resource.ref for resource in resources] == [HPA, NODEPOOL]
    assert {resource.controller for resource in resources} == {
        (ControllerKind.FLUX_KUSTOMIZATION, "flux-system", "platform")
    }


def test_a_resource_listed_by_one_controller_binds_to_its_resolved_location() -> None:
    source = _source()
    index = ResourceBindingIndex()
    index.add_argo_application(APPLICATION)
    index.add_sources([source])

    binding = index.resolve(PV)
    assert binding is not None and binding.resolved
    assert binding.controllers == ((ControllerKind.ARGOCD, "argocd", "payments-prod"),)
    assert binding.location == source
    # Cluster-scoped resources get the reserved cluster-scope namespace (never a real namespace).
    pv_source = binding.workload_source()
    assert pv_source is not None
    assert (pv_source.namespace, pv_source.workload_kind, pv_source.workload_name) == (
        CLUSTER_SCOPE_NAMESPACE,
        "PersistentVolume",
        "pv-orphan",
    )
    assert source_matches_target(pv_source, PV)
    assert pv_source.evidence[CONTROLLER_PRUNE_EVIDENCE] is None

    deployment = index.resolve(
        ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api")
    )
    assert deployment is not None
    deployment_source = deployment.workload_source()
    assert deployment_source is not None
    assert deployment_source.model_copy(update={"evidence": source.evidence}) == source

    hpa_binding = index.resolve(HPA)
    assert hpa_binding is not None
    hpa_source = hpa_binding.workload_source()
    assert hpa_source is not None
    assert (hpa_source.workload_kind, hpa_source.workload_name, hpa_source.namespace) == (
        "HorizontalPodAutoscaler",
        "api",
        "payments",
    )
    assert (hpa_source.repo_url, hpa_source.path, hpa_source.renderer) == (
        source.repo_url,
        source.path,
        source.renderer,
    )
    assert hpa_source.id != source.id


def test_lookup_is_version_insensitive() -> None:
    index = ResourceBindingIndex()
    index.add_argo_application(APPLICATION)

    assert index.resource_bindings(GroupVersionKind("autoscaling", "v1", "HorizontalPodAutoscaler"), "payments", "api")


def test_a_resource_listed_by_two_controllers_is_ambiguous_and_fails_closed() -> None:
    index = ResourceBindingIndex()
    index.add_argo_application(APPLICATION)
    index.add_flux_kustomization(KUSTOMIZATION)
    index.add_sources([_source()])

    binding = index.resolve(HPA)
    assert binding is not None and binding.status == "ambiguous" and not binding.resolved
    with pytest.raises(UnboundResourceError, match="several GitOps controllers"):
        bind_resources(index, [HPA])


def test_unmanaged_and_unresolved_resources_fail_closed_with_a_reason() -> None:
    index = ResourceBindingIndex()
    index.add_flux_kustomization(KUSTOMIZATION)

    assert index.resolve(PV) is None
    with pytest.raises(UnboundResourceError, match="not managed"):
        bind_resources(index, [PV])
    with pytest.raises(UnboundResourceError, match="unresolved"):
        bind_resources(index, [NODEPOOL])


def test_controllers_with_disagreeing_workload_locations_get_no_location() -> None:
    index = ResourceBindingIndex()
    index.add_argo_application(APPLICATION)
    index.add_sources(
        [
            _source(),
            _source(id="source-2", workload_name="worker", path="apps/other"),
            _source(id="source-3", workload_name="blocked", status=BindingStatus.BLOCKED, path="elsewhere"),
        ]
    )

    binding = index.resolve(PV)
    assert binding is not None and not binding.resolved
    assert binding.reason == "controller-source-unresolved"


def test_controller_prune_settings_are_recorded_per_controller() -> None:
    automated = {**APPLICATION, "spec": {**APPLICATION["spec"], "syncPolicy": {"automated": {"prune": True}}}}
    index = ResourceBindingIndex()
    index.add_argo_application(automated)
    index.add_sources([_source()])
    binding = index.resolve(PV)
    assert binding is not None and binding.prune is True
    source = binding.workload_source()
    assert source is not None and source.evidence[CONTROLLER_PRUNE_EVIDENCE] is True

    no_prune = {**APPLICATION, "spec": {**APPLICATION["spec"], "syncPolicy": {"automated": {}}}}
    index = ResourceBindingIndex()
    index.add_argo_application(no_prune)
    assert index.resolve(PV) is not None and index.resolve(PV).prune is False


def test_flux_prune_is_read_from_the_kustomization_spec() -> None:
    kustomization = {
        "metadata": {"name": "apps", "namespace": "flux-system"},
        "spec": {"prune": True},
        "status": {"inventory": {"entries": [{"id": "shop_web-lb__Service", "v": "v1"}]}},
    }
    index = ResourceBindingIndex()
    index.add_flux_kustomization(kustomization)
    binding = index.resolve(ResourceTarget(api_version="v1", kind="Service", namespace="shop", name="web-lb"))
    assert binding is not None and binding.prune is True


def test_bindings_are_listed_by_kind_and_round_trip_through_their_payload() -> None:
    index = ResourceBindingIndex()
    index.add_argo_application(APPLICATION)
    index.add_sources([_source()])

    bindings = index.bindings({("", "PersistentVolume")})
    assert [binding.ref for binding in bindings] == [PV]
    restored = ResourceBinding.from_payload(bindings[0].to_payload())
    assert restored == bindings[0]
    assert index.bindings({("", "ConfigMap")}) == []


def test_cluster_scope_sentinel_maps_back_to_an_empty_namespace() -> None:
    assert target_namespace(CLUSTER_SCOPE_NAMESPACE) == ""
    assert target_namespace("payments") == "payments"
