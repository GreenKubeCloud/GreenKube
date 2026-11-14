"""Delivery safety: discovery never binds through a remote destination or an unknown source type."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from greenkube.gitops.contracts import BindingStatus, RendererKind, ResourceTarget
from greenkube.gitops.discovery.argo import discover_argo_application
from greenkube.gitops.discovery.common import argo_destination_is_local, flux_targets_remote_cluster
from greenkube.gitops.discovery.inventory import (
    UnboundResourceError,
    bind_resources,
)
from greenkube.gitops.discovery.kubernetes import KubernetesDiscoverySource

PV = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-data")


class _CustomObjectsApi:
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = dict(responses)

    async def list_cluster_custom_object(self, group: str, version: str, plural: str, **kwargs: Any) -> object:
        return self.responses.get(plural, {"items": []})


def _application(
    *,
    name: str = "payments",
    destination: Mapping[str, Any] | None = None,
    source_type: str | None = "Directory",
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "sync": {"revision": "a" * 40},
        "resources": [
            {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "apps", "name": "api"},
            {"group": "", "version": "v1", "kind": "PersistentVolume", "name": "pv-data"},
        ],
    }
    if source_type is not None:
        status["sourceType"] = source_type
    return {
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {
            "destination": dict(destination or {"server": "https://kubernetes.default.svc", "namespace": "apps"}),
            "source": dict(
                source
                or {"repoURL": "https://github.com/acme/apps.git", "targetRevision": "main", "path": "apps/payments"}
            ),
            "syncPolicy": {"automated": {"prune": True}},
        },
        "status": status,
    }


def _kustomization(*, kube_config: bool = False) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "path": "./apps/payments",
        "prune": True,
        "sourceRef": {"kind": "GitRepository", "name": "gitops"},
    }
    if kube_config:
        spec["kubeConfig"] = {"secretRef": {"name": "spoke-kubeconfig"}}
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": "payments", "namespace": "flux-system"},
        "spec": spec,
        "status": {
            "inventory": {
                "entries": [
                    {"id": "apps_api_apps_Deployment", "v": "v1"},
                    {"id": "_pv-data__PersistentVolume", "v": "v1"},
                ]
            }
        },
    }


def _git_repository() -> dict[str, Any]:
    return {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": "gitops", "namespace": "flux-system"},
        "spec": {"url": "https://github.com/acme/gitops.git", "ref": {"branch": "main"}},
        "status": {"artifact": {"revision": f"main@sha1:{'b' * 40}"}},
    }


# -- (1) destination cluster ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("destination", "aliases", "local"),
    [
        ({"server": "https://kubernetes.default.svc"}, (), True),
        ({"server": "https://kubernetes.default.svc/"}, (), True),
        ({"name": "in-cluster"}, (), True),
        ({"server": "https://spoke.example.test:6443"}, (), False),
        ({"name": "spoke-eu"}, (), False),
        ({"server": "https://10.0.0.1:6443"}, ("https://10.0.0.1:6443/",), True),
        ({"name": "hub"}, ("hub",), True),
        ({"name": "hub"}, ("https://hub.example.test",), False),
    ],
)
def test_argo_destination_locality(destination, aliases, local):
    assert argo_destination_is_local(destination, aliases) is local


def test_flux_kube_config_targets_a_remote_cluster():
    assert flux_targets_remote_cluster({"kubeConfig": {"secretRef": {"name": "spoke"}}}) is True
    assert flux_targets_remote_cluster({"path": "./apps"}) is False


def test_hub_application_targeting_a_remote_server_never_binds_a_local_workload():
    application = _application(destination={"server": "https://spoke.example.test:6443", "namespace": "apps"})

    candidates = discover_argo_application(application, cluster="prod", provider_id="github-main")

    assert [candidate.workload_name for candidate in candidates] == ["api"]
    assert all(candidate.status is BindingStatus.BLOCKED for candidate in candidates)
    assert {candidate.reason for candidate in candidates} == {"destination_remote"}
    assert candidates[0].evidence["destination"] == {"server": "https://spoke.example.test:6443", "namespace": "apps"}


def test_a_configured_local_alias_keeps_the_application_bound():
    application = _application(destination={"name": "hub", "namespace": "apps"})

    blocked = discover_argo_application(application, cluster="prod", provider_id="github-main")
    bound = discover_argo_application(
        application, cluster="prod", provider_id="github-main", local_cluster_aliases=("hub",)
    )

    assert blocked[0].reason == "destination_remote"
    assert bound[0].status is BindingStatus.RESOLVED


@pytest.mark.asyncio
async def test_hub_application_never_binds_a_local_persistent_volume_for_deletion():
    remote = _application(destination={"server": "https://spoke.example.test:6443", "namespace": "apps"})
    source = KubernetesDiscoverySource(
        _CustomObjectsApi({"applications": {"items": [remote]}}), local_cluster_aliases=()
    )

    result = await source.discover(cluster="prod", provider_id="github-main")

    assert {candidate.reason for candidate in result.candidates} == {"destination_remote"}
    assert result.resource_index is not None
    result.resource_index.add_sources(result.candidates)
    binding = result.resource_index.resolve(PV)
    assert binding is not None and not binding.resolved and binding.reason == "destination_remote"
    assert binding.workload_source() is None
    with pytest.raises(UnboundResourceError, match="destination_remote"):
        bind_resources(result.resource_index, [PV])


@pytest.mark.asyncio
async def test_a_remote_application_makes_a_same_named_local_resource_ambiguous_not_resolved():
    remote = _application(name="spoke-payments", destination={"name": "spoke", "namespace": "apps"})
    local = _application()
    source = KubernetesDiscoverySource(
        _CustomObjectsApi({"applications": {"items": [remote, local]}}), local_cluster_aliases=()
    )

    result = await source.discover(cluster="prod", provider_id="github-main")

    assert result.resource_index is not None
    binding = result.resource_index.resolve(PV)
    assert binding is not None and binding.status == "ambiguous"


@pytest.mark.asyncio
async def test_flux_kustomization_with_a_kube_config_never_binds_local_workloads_or_resources():
    responses = {
        "kustomizations": {"items": [_kustomization(kube_config=True)]},
        "gitrepositories": {"items": [_git_repository()]},
    }
    source = KubernetesDiscoverySource(_CustomObjectsApi(responses), local_cluster_aliases=())

    result = await source.discover(cluster="prod", provider_id="github-main")

    assert result.candidates
    assert {candidate.status for candidate in result.candidates} == {BindingStatus.BLOCKED}
    assert {candidate.reason for candidate in result.candidates} == {"destination_remote"}
    assert result.candidates[0].evidence["destination"] == {"kube_config_secret": "spoke-kubeconfig"}
    assert result.resource_index is not None
    result.resource_index.add_sources(result.candidates)
    binding = result.resource_index.resolve(PV)
    assert binding is not None and binding.reason == "destination_remote" and not binding.resolved


@pytest.mark.asyncio
async def test_a_local_flux_kustomization_still_binds():
    responses = {
        "kustomizations": {"items": [_kustomization()]},
        "gitrepositories": {"items": [_git_repository()]},
    }
    source = KubernetesDiscoverySource(_CustomObjectsApi(responses), local_cluster_aliases=())

    result = await source.discover(cluster="prod", provider_id="github-main")

    assert {candidate.status for candidate in result.candidates} == {BindingStatus.RESOLVED}


def test_discovery_source_reads_the_configured_aliases(monkeypatch):
    from greenkube.core.config import Config

    monkeypatch.setattr(
        "greenkube.core.config.get_config",
        lambda: Config(GITOPS_LOCAL_CLUSTER_ALIASES='["https://10.0.0.1:6443", "hub"]'),
    )

    assert KubernetesDiscoverySource(object()).local_cluster_aliases == ("https://10.0.0.1:6443", "hub")


def test_invalid_local_cluster_aliases_are_rejected():
    from greenkube.core.config import Config

    with pytest.raises(ValueError):
        Config(GITOPS_LOCAL_CLUSTER_ALIASES='"hub"')
    with pytest.raises(ValueError):
        Config(GITOPS_LOCAL_CLUSTER_ALIASES='["", "hub"]')


# -- (5) unknown Argo CD source type ---------------------------------------------------------


def test_missing_source_type_is_unresolved_instead_of_raw():
    """Regression: a ComparisonError left status.sourceType unset and the overlay was bound as raw."""
    candidates = discover_argo_application(_application(source_type=None), cluster="prod", provider_id="github-main")

    assert candidates[0].status is BindingStatus.UNRESOLVED
    assert candidates[0].reason == "source_type_unknown"


def test_reported_kustomize_source_type_binds_the_kustomize_renderer():
    candidates = discover_argo_application(
        _application(source_type="Kustomize"), cluster="prod", provider_id="github-main"
    )

    assert candidates[0].status is BindingStatus.RESOLVED
    assert candidates[0].renderer is RendererKind.KUSTOMIZE


def test_reported_helm_source_type_on_a_git_path_binds_the_helm_renderer():
    candidates = discover_argo_application(_application(source_type="Helm"), cluster="prod", provider_id="github-main")

    assert candidates[0].renderer is RendererKind.HELM_VALUES


def test_reported_plugin_source_type_is_blocked():
    candidates = discover_argo_application(
        _application(source_type="Plugin"), cluster="prod", provider_id="github-main"
    )

    assert candidates[0].status is BindingStatus.BLOCKED
    assert candidates[0].reason == "unsupported-config-management-plugin"


def test_explicit_renderer_configuration_needs_no_reported_source_type():
    source = {
        "repoURL": "https://github.com/acme/apps.git",
        "targetRevision": "main",
        "path": "apps/payments",
        "directory": {"recurse": True},
    }
    candidates = discover_argo_application(
        _application(source_type=None, source=source), cluster="prod", provider_id="github-main"
    )

    assert candidates[0].status is BindingStatus.RESOLVED
    assert candidates[0].renderer is RendererKind.RAW


def test_multi_source_types_are_read_per_source():
    application = _application()
    application["spec"].pop("source")
    application["spec"]["sources"] = [
        {"repoURL": "https://github.com/acme/apps.git", "targetRevision": "main", "path": "apps/payments"},
    ]
    application["status"].pop("sourceType")
    application["status"]["sourceTypes"] = ["Kustomize"]

    candidates = discover_argo_application(application, cluster="prod", provider_id="github-main")

    assert candidates[0].renderer is RendererKind.KUSTOMIZE
