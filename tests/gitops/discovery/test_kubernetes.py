from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from greenkube.gitops.contracts import BindingStatus, ControllerKind
from greenkube.gitops.discovery import kubernetes as kubernetes_discovery
from greenkube.gitops.discovery.kubernetes import (
    KubernetesDiscoverySource,
    KubernetesWorkloadInventoryLimitError,
    KubernetesWorkloadInventoryReader,
)


class NoMatchError(Exception):
    pass


class _CustomObjectsApi:
    def __init__(self, responses: Mapping[str, object] | None = None) -> None:
        self.responses = dict(responses) if responses is not None else {}
        self.calls: list[tuple[str, str, str, str | None, str]] = []
        self.limits: list[int | None] = []

    async def list_cluster_custom_object(self, group: str, version: str, plural: str, **kwargs: Any) -> object:
        self.calls.append(("cluster", group, version, None, plural))
        self.limits.append(kwargs.get("limit"))
        return self._response(plural)

    async def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        **kwargs: Any,
    ) -> object:
        self.calls.append(("namespace", group, version, namespace, plural))
        self.limits.append(kwargs.get("limit"))
        return self._response(plural)

    def _response(self, plural: str) -> object:
        response = self.responses.get(plural, {"items": []})
        if isinstance(response, Exception):
            raise response
        return response


def _application() -> dict[str, Any]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "payments", "namespace": "argocd"},
        "spec": {
            "destination": {"namespace": "apps"},
            "source": {
                "repoURL": "https://github.com/acme/apps.git",
                "targetRevision": "main",
                "path": "apps/payments",
            },
        },
        "status": {
            "sync": {"revision": "a" * 40},
            "resources": [{"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "apps", "name": "api"}],
        },
    }


def _kustomization() -> dict[str, Any]:
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": "payments", "namespace": "flux-system"},
        "spec": {
            "path": "./apps/payments",
            "sourceRef": {"kind": "GitRepository", "name": "gitops"},
        },
        "status": {"inventory": {"entries": [{"id": "apps_v1_Deployment_apps_api"}]}},
    }


def _helm_release() -> dict[str, Any]:
    return {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": "payments", "namespace": "flux-system"},
        "spec": {
            "targetNamespace": "apps",
            "chart": {
                "spec": {
                    "chart": "payments",
                    "version": "1.2.3",
                    "sourceRef": {"kind": "HelmRepository", "name": "charts"},
                }
            },
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


def _helm_repository() -> dict[str, Any]:
    return {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "HelmRepository",
        "metadata": {"name": "charts", "namespace": "flux-system"},
        "spec": {"url": "https://charts.example.test"},
    }


def _helm_workload() -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "payments",
            "namespace": "apps",
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
            "annotations": {
                "meta.helm.sh/release-name": "apps-payments",
                "meta.helm.sh/release-namespace": "apps",
            },
        },
    }


class _AppsV1Api:
    def __init__(self, pages: Mapping[str, list[object]]) -> None:
        self.pages = dict(pages)
        self.page_indices = dict.fromkeys(pages, 0)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_deployment_for_all_namespaces(self, **kwargs: Any) -> object:
        return self._next_page("Deployment", kwargs)

    async def list_stateful_set_for_all_namespaces(self, **kwargs: Any) -> object:
        return self._next_page("StatefulSet", kwargs)

    async def list_daemon_set_for_all_namespaces(self, **kwargs: Any) -> object:
        return self._next_page("DaemonSet", kwargs)

    def _next_page(self, kind: str, kwargs: dict[str, Any]) -> object:
        self.calls.append((kind, kwargs))
        page_index = self.page_indices[kind]
        self.page_indices[kind] += 1
        return self.pages[kind][page_index]


def _list_page(items: list[object], continuation: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(items=items, metadata=SimpleNamespace(_continue=continuation))


def _api_workload(kind: str, name: str) -> object:
    metadata = client.V1ObjectMeta(
        name=name,
        namespace="apps",
        labels={"app.kubernetes.io/managed-by": "Helm"},
        annotations={
            "meta.helm.sh/release-name": "payments",
            "meta.helm.sh/release-namespace": "apps",
        },
    )
    workload_models = {
        "Deployment": client.V1Deployment,
        "StatefulSet": client.V1StatefulSet,
        "DaemonSet": client.V1DaemonSet,
    }
    return workload_models[kind](metadata=metadata)


@pytest.mark.asyncio
async def test_workload_inventory_reader_normalizes_bounded_paginated_apps_lists():
    api = _AppsV1Api(
        {
            "Deployment": [
                _list_page([_api_workload("Deployment", "api")], "deployments-page-2"),
                _list_page([_api_workload("Deployment", "worker")]),
            ],
            "StatefulSet": [_list_page([_api_workload("StatefulSet", "database")])],
            "DaemonSet": [_list_page([_api_workload("DaemonSet", "agent")])],
        }
    )

    resources = await KubernetesWorkloadInventoryReader(api, page_size=1, max_pages=2).list_workloads()

    assert [(resource["kind"], resource["metadata"]["name"]) for resource in resources] == [
        ("Deployment", "api"),
        ("Deployment", "worker"),
        ("StatefulSet", "database"),
        ("DaemonSet", "agent"),
    ]
    assert all(resource["apiVersion"] == "apps/v1" for resource in resources)
    assert resources[0]["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "Helm"}
    assert resources[0]["metadata"]["annotations"]["meta.helm.sh/release-name"] == "payments"
    assert [kind for kind, _ in api.calls] == ["Deployment", "Deployment", "StatefulSet", "DaemonSet"]
    assert all(kwargs["limit"] == 1 for _, kwargs in api.calls)
    assert api.calls[1][1]["_continue"] == "deployments-page-2"
    assert "_continue" not in api.calls[0][1]


@pytest.mark.asyncio
async def test_workload_inventory_reader_fails_if_the_page_limit_would_return_partial_data():
    api = _AppsV1Api(
        {
            "Deployment": [_list_page([_api_workload("Deployment", "api")], "more-deployments")],
            "StatefulSet": [],
            "DaemonSet": [],
        }
    )

    with pytest.raises(KubernetesWorkloadInventoryLimitError, match="Deployment"):
        await KubernetesWorkloadInventoryReader(api, page_size=1, max_pages=1).list_workloads()

    assert api.calls == [("Deployment", {"limit": 1})]


def _complete_responses() -> dict[str, object]:
    return {
        "applications": {"items": [_application()]},
        "kustomizations": {"items": [_kustomization()]},
        "helmreleases": {"items": [_helm_release()]},
        "gitrepositories": {"items": [_git_repository()]},
        "helmrepositories": {"items": [_helm_repository()]},
        "helmcharts": {"items": []},
    }


@pytest.mark.asyncio
async def test_cluster_wide_discovery_lists_crds_and_feeds_all_pure_adapters():
    api = _CustomObjectsApi(_complete_responses())

    result = await KubernetesDiscoverySource(api).discover(
        cluster="prod",
        provider_id="github-main",
        resources=[_helm_workload()],
    )

    assert [source.controller_kind for source in result.candidates] == [
        ControllerKind.ARGOCD,
        ControllerKind.FLUX_KUSTOMIZATION,
        ControllerKind.FLUX_HELM_RELEASE,
    ]
    assert all(capability.available for capability in result.availability.values())
    assert api.calls == [
        ("cluster", "argoproj.io", "v1alpha1", None, "applications"),
        ("cluster", "kustomize.toolkit.fluxcd.io", "v1", None, "kustomizations"),
        ("cluster", "helm.toolkit.fluxcd.io", "v2", None, "helmreleases"),
        ("cluster", "source.toolkit.fluxcd.io", "v1", None, "gitrepositories"),
        ("cluster", "source.toolkit.fluxcd.io", "v1", None, "ocirepositories"),
        ("cluster", "source.toolkit.fluxcd.io", "v1", None, "helmrepositories"),
        ("cluster", "source.toolkit.fluxcd.io", "v1", None, "buckets"),
        ("cluster", "source.toolkit.fluxcd.io", "v1", None, "helmcharts"),
    ]


@pytest.mark.asyncio
async def test_capability_probe_limits_each_custom_resource_list_to_one_object():
    api = _CustomObjectsApi(_complete_responses())

    availability = await KubernetesDiscoverySource(api).check_availability(namespace="argocd")

    assert len(availability) == 8
    assert all(capability.available for capability in availability.values())
    assert len(api.calls) == 8
    assert api.limits == [1] * 8
    assert all(call[0] == "namespace" and call[3] == "argocd" for call in api.calls)


@pytest.mark.asyncio
async def test_source_less_argo_application_does_not_abort_other_discovery_objects():
    malformed = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "source-less", "namespace": "argocd"},
        "status": {
            "resources": [
                {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "apps", "name": "orphan"},
            ]
        },
    }
    responses = _complete_responses()
    responses["applications"] = {"items": [malformed, _application()]}

    result = await KubernetesDiscoverySource(_CustomObjectsApi(responses)).discover(
        cluster="prod",
        provider_id="github-main",
    )

    sources = {source.workload_name: source for source in result.candidates}
    assert sources["orphan"].status is BindingStatus.BLOCKED
    assert sources["orphan"].reason == "workload-source-unresolved"
    assert sources["api"].status is BindingStatus.RESOLVED


@pytest.mark.asyncio
async def test_argo_adapter_exception_isolated_with_a_safe_blocked_candidate(monkeypatch):
    original = kubernetes_discovery.discover_argo_application

    def fail_one(application: Mapping[str, Any], **kwargs: Any):
        if application.get("metadata", {}).get("name") == "broken":
            raise RuntimeError("controller payload must not be exposed")
        return original(application, **kwargs)

    monkeypatch.setattr(kubernetes_discovery, "discover_argo_application", fail_one)
    malformed = {
        "metadata": {"name": "broken", "namespace": "argocd"},
        "spec": {"destination": {"namespace": "apps"}},
        "status": {
            "resources": [
                {"kind": "Deployment", "namespace": "apps", "name": "api"},
            ]
        },
    }
    responses = _complete_responses()
    responses["applications"] = {"items": [malformed, _application()]}

    result = await KubernetesDiscoverySource(_CustomObjectsApi(responses)).discover(
        cluster="prod",
        provider_id="github-main",
    )

    valid_candidate = next(source for source in result.candidates if source.controller_name == "payments")
    failed_candidate = next(source for source in result.candidates if source.controller_name == "broken")
    assert valid_candidate.status is BindingStatus.RESOLVED
    assert valid_candidate.reason is None
    assert failed_candidate.status is BindingStatus.BLOCKED
    assert failed_candidate.reason == "discovery-adapter-error"
    assert "controller payload must not be exposed" not in repr(failed_candidate.model_dump())


@pytest.mark.asyncio
async def test_flux_current_inventory_resolves_kustomize_and_git_owned_helm_release():
    flux_kustomize = _kustomization()
    flux_kustomize["metadata"]["name"] = "qa-flux-kustomize"
    flux_kustomize["spec"]["path"] = "./apps/flux-kustomize"
    flux_kustomize["spec"]["sourceRef"]["name"] = "qa-git"
    flux_kustomize["status"]["inventory"]["entries"] = [
        {
            "id": "greenkube-qa-flux-kustomize_flux-kustomize_apps_Deployment",
            "v": "v1",
        }
    ]

    flux_helm_kustomize = _kustomization()
    flux_helm_kustomize["metadata"]["name"] = "qa-flux-helm"
    flux_helm_kustomize["spec"]["path"] = "./clusters/minikube/flux-helm"
    flux_helm_kustomize["spec"]["sourceRef"]["name"] = "qa-git"
    flux_helm_kustomize["status"]["inventory"]["entries"] = [
        {
            "id": "flux-system_qa-flux-helm_helm.toolkit.fluxcd.io_HelmRelease",
            "v": "v2",
        },
        {"id": "flux-system_qa-values__ConfigMap", "v": "v1"},
    ]

    helm_release = _helm_release()
    helm_release["metadata"]["name"] = "qa-flux-helm"
    helm_release["spec"]["releaseName"] = "qa-flux-helm"
    helm_release["spec"]["targetNamespace"] = "greenkube-qa-flux-helm"
    helm_release["spec"]["values"] = {"replicaCount": 1}
    helm_release["spec"].pop("chart")
    helm_release["spec"]["chartRef"] = {
        "kind": "HelmChart",
        "name": "qa-flux-helm-chart",
        "namespace": "flux-system",
    }
    helm_release["spec"]["valuesFrom"] = [{"kind": "ConfigMap", "name": "qa-values"}]

    git_repository = _git_repository()
    git_repository["metadata"]["name"] = "qa-git"
    git_repository["spec"]["url"] = "http://gitea.example.test/greenkube-qa/greenkube-qa-gitops.git"
    helm_chart = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "HelmChart",
        "metadata": {"name": "qa-flux-helm-chart", "namespace": "flux-system", "uid": "chart-uid"},
        "spec": {
            "chart": "./apps/flux-helm",
            "version": "1.2.3",
            "sourceRef": {"kind": "GitRepository", "name": "qa-git", "namespace": "flux-system"},
        },
    }

    flux_workload = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "flux-kustomize", "namespace": "greenkube-qa-flux-kustomize"},
    }
    helm_workload = _helm_workload()
    helm_workload["metadata"]["name"] = "qa-flux-helm"
    helm_workload["metadata"]["namespace"] = "greenkube-qa-flux-helm"
    helm_workload["metadata"]["annotations"].update(
        {
            "meta.helm.sh/release-name": "qa-flux-helm",
            "meta.helm.sh/release-namespace": "greenkube-qa-flux-helm",
        }
    )
    responses = {
        "applications": {"items": []},
        "kustomizations": {"items": [flux_kustomize, flux_helm_kustomize]},
        "helmreleases": {"items": [helm_release]},
        "gitrepositories": {"items": [git_repository]},
        "helmcharts": {"items": [helm_chart]},
    }

    result = await KubernetesDiscoverySource(_CustomObjectsApi(responses)).discover(
        cluster="minikube",
        provider_id="gitea-qa",
        resources=[flux_workload, helm_workload],
    )

    by_workload = {source.workload_name: source for source in result.candidates}
    kustomize_source = by_workload["flux-kustomize"]
    helm_source = by_workload["qa-flux-helm"]

    assert kustomize_source.controller_kind is ControllerKind.FLUX_KUSTOMIZATION
    assert kustomize_source.status is BindingStatus.RESOLVED
    assert kustomize_source.repo_url == git_repository["spec"]["url"]
    assert kustomize_source.path == "apps/flux-kustomize"
    assert helm_source.controller_kind is ControllerKind.FLUX_HELM_RELEASE
    assert helm_source.status is BindingStatus.RESOLVED
    assert helm_source.repo_url == git_repository["spec"]["url"]
    assert helm_source.path == "clusters/minikube/flux-helm"
    assert helm_source.renderer_config["manifest_source"]["path"] == "clusters/minikube/flux-helm"
    values_from = helm_source.renderer_config["values_from"]
    assert values_from[0]["git_managed"] is True
    assert values_from[0]["namespace"] == "flux-system"
    assert values_from[0]["path"] == "clusters/minikube/flux-helm"
    assert helm_source.evidence["chart_ref"]["uid"] == "chart-uid"


@pytest.mark.asyncio
async def test_namespace_scoped_discovery_lists_every_crd_in_the_requested_namespace():
    api = _CustomObjectsApi()

    result = await KubernetesDiscoverySource(api).discover(
        cluster="prod",
        provider_id="github-main",
        namespace="team-a",
    )

    assert result.candidates == []
    assert len(api.calls) == 8
    assert all(call[0] == "namespace" and call[3] == "team-a" for call in api.calls)


@pytest.mark.asyncio
async def test_missing_crds_have_per_capability_availability_and_do_not_stop_other_lists():
    responses = _complete_responses()
    responses["applications"] = ApiException(status=404, reason="Not Found")
    responses["kustomizations"] = NoMatchError("resource is not served")
    responses["gitrepositories"] = ApiException(status=404, reason="Not Found")
    api = _CustomObjectsApi(responses)

    result = await KubernetesDiscoverySource(api).discover(cluster="prod", provider_id="github-main")

    assert result.availability["argocd"].available is False
    assert result.availability["argocd"].reason == "crd-not-found"
    assert result.availability["flux_kustomization"].available is False
    assert result.availability["flux_kustomization"].reason == "crd-not-found"
    assert result.availability["flux_helm_release"].available is True
    assert result.availability["flux_source_git_repository"].available is False
    assert result.availability["flux_source_git_repository"].reason == "crd-not-found"
    assert len(api.calls) == 8


@pytest.mark.asyncio
async def test_missing_flux_source_crd_keeps_kustomization_candidate_unresolved():
    responses = _complete_responses()
    responses["applications"] = {"items": []}
    responses["helmreleases"] = {"items": []}
    responses["gitrepositories"] = ApiException(status=404, reason="Not Found")
    api = _CustomObjectsApi(responses)

    result = await KubernetesDiscoverySource(api).discover(cluster="prod", provider_id="github-main")

    assert result.availability["flux_kustomization"].available is True
    assert result.availability["flux_source_git_repository"].available is False
    assert len(result.candidates) == 1
    assert result.candidates[0].status is BindingStatus.UNRESOLVED
    assert result.candidates[0].reason == "source-ref-unresolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "plural", "source_spec", "expected_url"),
    [
        (
            "GitRepository",
            "gitrepositories",
            {"url": "https://github.com/acme/gitops.git", "ref": {"branch": "main"}},
            "https://github.com/acme/gitops.git",
        ),
        (
            "OCIRepository",
            "ocirepositories",
            {"url": "oci://registry.example/acme", "ref": {"tag": "v1.2.3"}},
            "oci://registry.example/acme",
        ),
        (
            "HelmRepository",
            "helmrepositories",
            {"url": "https://charts.example.test"},
            "https://charts.example.test",
        ),
        (
            "Bucket",
            "buckets",
            {"endpoint": "objects.example.test", "bucketName": "manifests", "insecure": True},
            "http://objects.example.test/manifests",
        ),
    ],
)
async def test_flux_source_ref_kinds_are_listed_and_passed_to_the_adapter(
    kind: str,
    plural: str,
    source_spec: dict[str, Any],
    expected_url: str,
):
    kustomization = _kustomization()
    kustomization["spec"]["sourceRef"] = {"kind": kind, "name": "source"}
    responses = {
        "applications": {"items": []},
        "kustomizations": {"items": [kustomization]},
        "helmreleases": {"items": []},
        "gitrepositories": {"items": []},
        "ocirepositories": {"items": []},
        "helmrepositories": {"items": []},
        "buckets": {"items": []},
    }
    responses[plural] = {
        "items": [
            {
                "apiVersion": "source.toolkit.fluxcd.io/v1",
                "kind": kind,
                "metadata": {"name": "source", "namespace": "flux-system"},
                "spec": source_spec,
            }
        ]
    }
    result = await KubernetesDiscoverySource(_CustomObjectsApi(responses)).discover(
        cluster="prod",
        provider_id="github-main",
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].status is BindingStatus.RESOLVED
    assert result.candidates[0].repo_url == expected_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ApiException(status=403, reason="Forbidden"),
        ApiException(status=500, reason="Internal Server Error"),
        OSError("network unavailable"),
    ],
    ids=["forbidden", "server-error", "network-error"],
)
async def test_non_crd_errors_are_not_reported_as_missing_crds(error: Exception):
    api = _CustomObjectsApi({"applications": error})

    with pytest.raises(type(error)):
        await KubernetesDiscoverySource(api).discover(cluster="prod", provider_id="github-main")


class _AutoscalingApi:
    def __init__(self, hp_as: list[dict[str, Any]]) -> None:
        self.hp_as = hp_as

    async def list_horizontal_pod_autoscaler_for_all_namespaces(self, **kwargs: Any) -> object:
        return {"items": self.hp_as}


class _PolicyApi:
    def __init__(self, pdbs: list[dict[str, Any]]) -> None:
        self.pdbs = pdbs

    async def list_pod_disruption_budget_for_all_namespaces(self, **kwargs: Any) -> object:
        return {"items": self.pdbs}


@pytest.mark.asyncio
async def test_workload_inventory_collects_hpa_and_pdb_interaction_evidence():
    api = _AppsV1Api(
        {
            "Deployment": [_list_page([_api_workload("Deployment", "api")])],
            "StatefulSet": [_list_page([])],
            "DaemonSet": [_list_page([])],
        }
    )
    reader = KubernetesWorkloadInventoryReader(
        api,
        autoscaling_v2_api=_AutoscalingApi(
            [
                {
                    "metadata": {"name": "api-hpa", "namespace": "apps"},
                    "spec": {
                        "scaleTargetRef": {"kind": "Deployment", "name": "api"},
                        "minReplicas": 2,
                        "maxReplicas": 6,
                        "metrics": [{"type": "Resource"}],
                    },
                }
            ]
        ),
        policy_v1_api=_PolicyApi(
            [
                {
                    "metadata": {"name": "api-pdb", "namespace": "apps"},
                    "spec": {
                        "maxUnavailable": 0,
                        "selector": {"matchLabels": {"app.kubernetes.io/managed-by": "Helm"}},
                    },
                }
            ]
        ),
    )

    resources = await reader.list_workloads()

    evidence = resources[0]["_greenkube_policy_evidence"]
    assert evidence["checked"] is True
    assert evidence["hpa_targets"][0]["target_name"] == "api"
    assert evidence["pod_disruption_budgets"][0]["max_unavailable"] == 0


@pytest.mark.asyncio
async def test_discovery_attaches_live_labels_rollout_and_hpa_pdb_evidence():
    api = _CustomObjectsApi(_complete_responses())
    workload = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "api",
            "namespace": "apps",
            "generation": 3,
            "labels": {"maintenance": "true"},
        },
        "spec": {"replicas": 2, "template": {"metadata": {"labels": {"app": "payments"}}}},
        "status": {
            "observedGeneration": 3,
            "replicas": 2,
            "updatedReplicas": 1,
            "readyReplicas": 1,
        },
        "_greenkube_policy_evidence": {
            "checked": True,
            "hpa_targets": [
                {
                    "name": "api-hpa",
                    "namespace": "apps",
                    "target_kind": "Deployment",
                    "target_name": "api",
                    "metric_types": ["Resource"],
                }
            ],
            "pod_disruption_budgets": [],
        },
    }

    result = await KubernetesDiscoverySource(api).discover(
        cluster="prod",
        provider_id="github-main",
        resources=[workload],
    )

    argo_source = next(source for source in result.candidates if source.controller_kind is ControllerKind.ARGOCD)
    assert argo_source.evidence["workload_labels"] == {"app": "payments", "maintenance": "true"}
    assert argo_source.evidence["rollout_in_progress"] is True
    assert argo_source.evidence["hpa_pdb_interactions_checked"] is True
    assert argo_source.evidence["hpa_pdb_conflict"] is True
    assert argo_source.evidence["controller_suspended"] is False


@pytest.mark.asyncio
async def test_discovered_freeze_label_evidence_blocks_policy_without_hiding_other_workloads():
    from greenkube.automation.policy import AutomationPolicyConfig, AutomationPolicyEvaluator
    from greenkube.gitops.bindings.resolver import WorkloadBindingResolver
    from greenkube.models.evidence import RecommendationEvidence
    from greenkube.models.metrics import RecommendationRecord, RecommendationType

    def application(name: str, workload_name: str) -> dict[str, Any]:
        return {
            "metadata": {"name": name, "namespace": "argocd"},
            "spec": {
                "destination": {"namespace": "apps"},
                "source": {
                    "repoURL": "https://github.com/acme/apps.git",
                    "targetRevision": "main",
                    "path": f"apps/{workload_name}",
                },
            },
            "status": {
                "sync": {"revision": "a" * 40},
                "resources": [
                    {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "apps", "name": workload_name}
                ],
            },
        }

    def workload(name: str, labels: dict[str, str]) -> dict[str, Any]:
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": "apps", "generation": 1, "labels": labels},
            "spec": {"replicas": 1, "template": {"metadata": {"labels": {}}}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1},
        }

    api = _CustomObjectsApi(
        {
            **_complete_responses(),
            "applications": {"items": [application("frozen-app", "frozen"), application("open-app", "open")]},
        }
    )
    discovery = await KubernetesDiscoverySource(api).discover(
        cluster="prod",
        provider_id="github-main",
        resources=[workload("frozen", {"maintenance": "true"}), workload("open", {"maintenance": "false"})],
    )
    resolver = WorkloadBindingResolver()
    recommendations = [
        RecommendationRecord(
            id=index,
            namespace="apps",
            owner_kind="Deployment",
            owner_name=name,
            scope="workload",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Reduce CPU.",
            risk_level="low",
            confidence=0.9,
            evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
        )
        for index, name in enumerate(("frozen", "open"), start=1)
    ]

    class Plans:
        async def list_plans(self, *, status=None, source_id=None):
            return []

        async def get_plan_record(self, plan_id):
            return None

    evaluator = AutomationPolicyEvaluator(
        config=AutomationPolicyConfig(change_freeze_labels=("maintenance=true",)),
        plan_repository=Plans(),
    )
    frozen_source = resolver.resolve(
        [source for source in discovery.candidates if source.workload_name == "frozen"]
    ).source
    open_source = resolver.resolve([source for source in discovery.candidates if source.workload_name == "open"]).source

    assert frozen_source is not None and open_source is not None
    frozen_result = await evaluator.evaluate([recommendations[0]], frozen_source)
    open_result = await evaluator.evaluate([recommendations[1]], open_source)

    assert frozen_result.allowed is False
    assert any(
        check.gate == "change_freeze_labels" and check.state.value == "rejected" for check in frozen_result.checks
    )
    assert open_result.allowed is True
