import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from greenkube.gitops.contracts import BindingStatus, ControllerKind, RendererKind, WorkloadSource
from greenkube.gitops.discovery.kubernetes import (
    CapabilityAvailability,
    KubernetesDiscoverySource,
)
from greenkube.gitops.discovery.runtime import (
    ConfiguredKubernetesDiscoverySource,
    ConfiguredRepositoryProviderResolver,
    GitOpsDiscoveryRuntime,
    create_gitops_discovery_runtime,
)
from greenkube.gitops.discovery.service import (
    DiscoveryCoverage,
    DiscoveryRefreshResult,
    GitOpsDiscoveryService,
)
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderConfig,
    WorkloadBinding,
    WorkloadBindingCoverage,
    WorkloadBindingPage,
    WorkloadBindingRepository,
)

_CAPABILITIES = (
    "argocd",
    "flux_kustomization",
    "flux_helm_release",
    "flux_source_git_repository",
    "flux_source_oci_repository",
    "flux_source_helm_repository",
    "flux_source_bucket",
    "flux_source_helm_chart",
)
_NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


class _ProviderConfig:
    def __init__(
        self,
        provider_id: str,
        *,
        provider_type: str = "github",
        api_base_url: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.id = provider_id
        self.provider_type = provider_type
        self.api_base_url = api_base_url
        self.enabled = enabled

    @property
    def secret_name(self) -> str:
        raise AssertionError("discovery must not read provider credentials")

    @property
    def secret_key(self) -> str:
        raise AssertionError("discovery must not read provider credentials")


class _MemoryRepository:
    def __init__(
        self,
        providers: list[Any] | None = None,
        allowlists: Mapping[str, list[str]] | None = None,
    ) -> None:
        self.providers = list(providers or [])
        self.allowlists = dict(allowlists or {})
        self.bindings: dict[tuple[str, str, str, str], WorkloadBinding] = {}
        self.provider_list_calls: list[bool] = []
        self.allowlist_calls: list[str] = []
        self.save_count = 0

    async def list_provider_configs(self, *, enabled_only: bool = False) -> list[Any]:
        self.provider_list_calls.append(enabled_only)
        if enabled_only:
            return [provider for provider in self.providers if provider.enabled]
        return list(self.providers)

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        return next((provider for provider in self.providers if provider.id == provider_id), None)

    async def save_provider_config(self, provider: GitOpsProviderConfig) -> GitOpsProviderConfig:
        self.providers = [current for current in self.providers if current.id != provider.id]
        self.providers.append(provider)
        return provider

    async def add_provider_allowlist(self, provider_id: str, repository_pattern: str) -> None:
        self.allowlists.setdefault(provider_id, []).append(repository_pattern)

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        self.allowlist_calls.append(provider_id)
        return list(self.allowlists.get(provider_id, ()))

    async def remove_provider_allowlist(self, provider_id: str, repository_pattern: str) -> bool:
        patterns = self.allowlists.get(provider_id, [])
        if repository_pattern not in patterns:
            return False
        patterns.remove(repository_pattern)
        return True

    async def save_workload_binding(self, binding: WorkloadBinding) -> WorkloadBinding:
        self.save_count += 1
        identity = _binding_identity(binding)
        current = self.bindings.get(identity)
        saved = replace(binding, generation=(current.generation + 1 if current is not None else 1))
        self.bindings[identity] = saved
        return saved

    async def list_workload_bindings(self, *, cluster: str | None = None) -> list[WorkloadBinding]:
        return [binding for binding in self.bindings.values() if cluster is None or binding.cluster == cluster]

    async def list_workload_bindings_page(
        self,
        *,
        cluster: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        statuses=None,
        candidate_limit: int = 50,
        now: datetime | None = None,
    ) -> WorkloadBindingPage:
        if cursor is not None or limit < 1 or candidate_limit < 1:
            raise AssertionError("these runtime tests use a single first-page binding projection")
        items = sorted(
            (binding for binding in self.bindings.values() if cluster is None or binding.cluster == cluster),
            key=_binding_identity,
        )
        if statuses is not None:
            selected = {BindingStatus(status) for status in statuses}
            items = [binding for binding in items if binding.status in selected]
        return WorkloadBindingPage(tuple(items[:limit]), None, False)

    async def get_workload_binding_coverage(
        self,
        *,
        cluster: str | None = None,
        now: datetime | None = None,
    ) -> WorkloadBindingCoverage:
        items = await self.list_workload_bindings(cluster=cluster)
        return WorkloadBindingCoverage.from_bindings(items)

    async def save_discovered_workload_binding(self, binding, *, expected_generation, events=()):
        if events:
            raise AssertionError("these runtime tests do not exercise binding events")
        current = await self.get_workload_binding(
            binding.cluster,
            binding.namespace,
            binding.workload_kind,
            binding.workload_name,
        )
        if (current.generation if current is not None else None) != expected_generation:
            return None
        return await self.save_workload_binding(binding)

    async def get_workload_binding(
        self,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
    ) -> WorkloadBinding | None:
        return self.bindings.get((cluster, namespace, workload_kind, workload_name))


class _CustomObjectsApi:
    def __init__(
        self,
        applications: list[Mapping[str, Any]] | None = None,
        errors: Mapping[str, Exception] | None = None,
        responses: Mapping[str, object] | None = None,
    ) -> None:
        self.applications = list(applications or [])
        self.errors = dict(errors or {})
        self.responses = dict(responses or {})
        self.calls: list[str] = []

    async def list_cluster_custom_object(self, *, plural: str, **kwargs: Any) -> object:
        self.calls.append(plural)
        if plural in self.errors:
            raise self.errors[plural]
        if plural in self.responses:
            return self.responses[plural]
        if plural == "applications":
            return {"items": self.applications}
        return {"items": []}


class _AppsV1Api:
    def __init__(
        self,
        deployments: list[Mapping[str, Any]] | None = None,
        *,
        failures: list[Exception] | None = None,
        on_daemonsets: Any | None = None,
    ) -> None:
        self.deployments = list(deployments or [])
        self.failures = list(failures or [])
        self.on_daemonsets = on_daemonsets
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_deployment_for_all_namespaces(self, **kwargs: Any) -> object:
        return self._response("Deployment", kwargs)

    async def list_stateful_set_for_all_namespaces(self, **kwargs: Any) -> object:
        return self._response("StatefulSet", kwargs)

    async def list_daemon_set_for_all_namespaces(self, **kwargs: Any) -> object:
        return self._response("DaemonSet", kwargs)

    def _response(self, kind: str, kwargs: dict[str, Any]) -> object:
        self.calls.append((kind, kwargs))
        if self.failures:
            raise self.failures.pop(0)
        if kind == "DaemonSet" and self.on_daemonsets is not None:
            self.on_daemonsets()
        items = self.deployments if kind == "Deployment" else []
        return {"items": items, "metadata": {"continue": ""}}


def _application(repo_url: str = "https://github.example.test/acme/gitops.git") -> dict[str, Any]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "payments", "namespace": "argocd"},
        "spec": {
            "destination": {"namespace": "payments"},
            "source": {"repoURL": repo_url, "targetRevision": "main", "path": "apps/payments"},
        },
        "status": {
            "sync": {"revision": "a" * 40},
            "resources": [
                {
                    "group": "apps",
                    "version": "v1",
                    "kind": "Deployment",
                    "namespace": "payments",
                    "name": "api",
                }
            ],
        },
    }


def _helm_release() -> dict[str, Any]:
    return {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": "payments", "namespace": "flux-system"},
        "spec": {
            "releaseName": "payments",
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
                "meta.helm.sh/release-name": "payments",
                "meta.helm.sh/release-namespace": "apps",
            },
        },
    }


def _provider(provider_id: str, *, api_base_url: str | None = None, enabled: bool = True) -> GitOpsProviderConfig:
    return GitOpsProviderConfig(
        id=provider_id,
        provider_type="github",
        api_base_url=api_base_url,
        secret_namespace="greenkube",
        secret_name="git-token",
        secret_key="token",
        enabled=enabled,
    )


def _binding_identity(binding: WorkloadBinding) -> tuple[str, str, str, str]:
    return binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["acme/gitops", "acme/*"])
async def test_configured_source_matches_enabled_provider_allowlists_without_reading_credentials(pattern: str):
    provider = _ProviderConfig("github-main", api_base_url="https://github.example.test/api/v3")
    repository = _MemoryRepository([provider], {"github-main": [pattern]})
    source = ConfiguredKubernetesDiscoverySource(
        KubernetesDiscoverySource(_CustomObjectsApi([_application()])),
        ConfiguredRepositoryProviderResolver(repository),
    )

    result = await source.discover(cluster="prod", provider_id="unresolved")

    assert len(result.candidates) == 1
    assert result.candidates[0].provider_id == "github-main"
    assert result.candidates[0].status is BindingStatus.RESOLVED
    assert result.availability.keys() == set(_CAPABILITIES)
    assert all(capability.available for capability in result.availability.values())
    assert repository.provider_list_calls == [True]
    assert repository.allowlist_calls == ["github-main"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "api_base_url", "repo_url", "allowlist"),
    [
        (
            "github",
            "https://api.github.com",
            "https://github.com/acme/gitops.git",
            "acme/gitops",
        ),
        (
            "gitlab",
            "https://gitlab.example.test/api/v4",
            "https://gitlab.example.test/platform/team/gitops.git",
            "platform/team/gitops",
        ),
        (
            "gitea",
            "https://gitea.example.test/api/v1",
            "git@gitea.example.test:acme/gitops.git",
            "acme/*",
        ),
    ],
)
async def test_configured_source_supports_enabled_github_gitlab_and_gitea_providers(
    provider_type: str,
    api_base_url: str,
    repo_url: str,
    allowlist: str,
):
    provider = _ProviderConfig("provider", provider_type=provider_type, api_base_url=api_base_url)
    repository = _MemoryRepository([provider], {"provider": [allowlist]})
    source = ConfiguredKubernetesDiscoverySource(
        KubernetesDiscoverySource(_CustomObjectsApi([_application(repo_url)])),
        ConfiguredRepositoryProviderResolver(repository),
    )

    result = await source.discover(cluster="prod", provider_id="unresolved")

    assert len(result.candidates) == 1
    assert result.candidates[0].provider_id == "provider"
    assert result.candidates[0].status is BindingStatus.RESOLVED


@pytest.mark.asyncio
async def test_provider_resolution_updates_same_repository_flux_manifest_ownership():
    repo_url = "https://gitea.example.test/owner/repo.git"
    provider = _ProviderConfig(
        "gitea-qa",
        provider_type="gitea",
        api_base_url="https://gitea.example.test/api/v1",
    )
    repository = _MemoryRepository([provider], {"gitea-qa": ["owner/repo"]})
    source = WorkloadSource(
        id="flux_helm_release-source",
        cluster="minikube",
        namespace="qa-apps",
        workload_kind="Deployment",
        workload_name="qa-flux-helm",
        provider_id="unresolved",
        repo_url=repo_url,
        ref="main",
        source_revision=f"main@sha1:{'a' * 40}",
        path="clusters/minikube/flux-helm",
        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
        controller_name="qa-flux-helm",
        controller_namespace="flux-system",
        renderer=RendererKind.HELM_RELEASE,
        renderer_config={
            "manifest_source": {
                "provider_id": "unresolved",
                "repo_url": repo_url,
                "ref": "main",
                "path": "clusters/minikube/flux-helm",
            },
            "application_source": {
                "provider_id": "unresolved",
                "repo_url": "https://other.example.test/team/apps.git",
                "ref": "main",
                "path": ".",
            },
        },
        confidence=0.2,
        status=BindingStatus.RESOLVED,
        evidence={},
    )

    resolved = await ConfiguredRepositoryProviderResolver(repository).resolve_candidates([source])

    assert len(resolved) == 1
    assert resolved[0].provider_id == "gitea-qa"
    assert resolved[0].renderer_config["manifest_source"]["provider_id"] == "gitea-qa"
    assert resolved[0].renderer_config["application_source"]["provider_id"] == "unresolved"


@pytest.mark.asyncio
async def test_configured_source_blocks_repo_without_enabled_provider_match_with_safe_reason():
    disabled = _provider("disabled", api_base_url="https://github.example.test/api/v3", enabled=False)
    other_host = _provider("other-host", api_base_url="https://elsewhere.example.test/api/v3")
    repository = _MemoryRepository(
        [disabled, other_host],
        {"disabled": ["acme/*"], "other-host": ["acme/*"]},
    )
    source = ConfiguredKubernetesDiscoverySource(
        KubernetesDiscoverySource(_CustomObjectsApi([_application()])),
        ConfiguredRepositoryProviderResolver(repository),
    )

    result = await source.discover(cluster="prod", provider_id="unresolved")
    candidate = result.candidates[0]

    assert candidate.provider_id == "unresolved"
    assert candidate.status is BindingStatus.BLOCKED
    assert candidate.reason == "provider-unresolved"
    assert "github.example.test" not in candidate.reason
    assert repository.allowlist_calls == ["other-host"]


@pytest.mark.asyncio
async def test_configured_source_preserves_missing_crd_availability():
    repository = _MemoryRepository()
    source = ConfiguredKubernetesDiscoverySource(
        KubernetesDiscoverySource(
            _CustomObjectsApi(errors={"applications": ApiException(status=404, reason="Not Found")})
        ),
        ConfiguredRepositoryProviderResolver(repository),
    )

    result = await source.discover(cluster="prod", provider_id="unresolved")

    assert result.candidates == []
    assert result.availability["argocd"].available is False
    assert result.availability["argocd"].reason == "crd-not-found"
    assert all(result.availability[name].available for name in _CAPABILITIES if name != "argocd")


@pytest.mark.asyncio
async def test_multiple_provider_matches_remain_ambiguous_with_distinct_candidate_ids():
    first = _provider("github-main", api_base_url="https://github.example.test/api/v3")
    second = _provider("github-backup", api_base_url="https://github.example.test/api/v3")
    repository = _MemoryRepository(
        [first, second],
        {"github-main": ["acme/gitops"], "github-backup": ["acme/*"]},
    )
    api = _CustomObjectsApi([_application()])
    source = ConfiguredKubernetesDiscoverySource(
        KubernetesDiscoverySource(api),
        ConfiguredRepositoryProviderResolver(repository),
    )

    result = await source.discover(cluster="prod", provider_id="unresolved")
    binding_repository = _MemoryRepository()
    service = GitOpsDiscoveryService(
        source,
        cast(WorkloadBindingRepository, binding_repository),
        cluster="prod",
        provider_id="unresolved",
    )
    refreshed = await service.refresh()

    assert {candidate.provider_id for candidate in result.candidates} == {"github-main", "github-backup"}
    assert all(candidate.status is BindingStatus.AMBIGUOUS for candidate in result.candidates)
    assert len({candidate.id for candidate in result.candidates}) == 2
    assert refreshed.coverage.ambiguous == 1
    assert refreshed.bindings[0].status is BindingStatus.AMBIGUOUS
    assert len(refreshed.bindings[0].candidates) == 2


@pytest.mark.asyncio
async def test_runtime_refresh_is_idempotent_and_keeps_service_stale_reconciliation():
    provider = _provider("github-main", api_base_url="https://github.example.test/api/v3")
    repository = _MemoryRepository([provider], {"github-main": ["acme/*"]})
    api = _CustomObjectsApi([_application()])
    runtime = await create_gitops_discovery_runtime(
        repository=repository,
        custom_objects_api=api,
        apps_v1_api=_AppsV1Api(),
        cluster="prod",
    )

    first = await runtime.refresh()
    second = await runtime.refresh()
    original_candidate_id = first.bindings[0].candidates[0].id
    api.applications = []
    stale = await runtime.refresh()

    assert first.coverage.resolved == second.coverage.resolved == 1
    assert second.bindings[0].candidates[0].id == original_candidate_id
    assert stale.bindings[0].status is BindingStatus.STALE
    assert stale.bindings[0].candidates[0].status is BindingStatus.STALE
    assert repository.save_count == 3


@pytest.mark.asyncio
async def test_runtime_registers_disabled_unresolved_provider_before_persisting_unmatched_sources():
    repository = _MemoryRepository()
    runtime = await create_gitops_discovery_runtime(
        repository=repository,
        custom_objects_api=_CustomObjectsApi([_application("https://unconfigured.example.test/team/repo.git")]),
        apps_v1_api=_AppsV1Api(),
        cluster="prod",
    )

    sentinel = next(provider for provider in repository.providers if provider.id == "unresolved")
    assert sentinel.provider_type == "gitea"
    assert sentinel.enabled is False
    result = await runtime.refresh()
    binding = repository.bindings[("prod", "payments", "Deployment", "api")]

    assert result.coverage.blocked == 1
    assert binding.status is BindingStatus.BLOCKED
    assert binding.candidates[0].provider_id == "unresolved"
    assert binding.candidates[0].reason == "provider-unresolved"


@pytest.mark.asyncio
async def test_runtime_reconciles_provider_settings_before_first_discovery(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "GITOPS_PROVIDERS_JSON",
        json.dumps(
            {
                "providers": [
                    {
                        "id": "github-main",
                        "type": "github",
                        "apiBaseUrl": "https://github.example.test/api/v3",
                        "tokenSecret": {"namespace": "greenkube-qa", "name": "git-provider", "key": "token"},
                        "repositories": ["acme/gitops"],
                    }
                ]
            }
        ),
    )
    repository = _MemoryRepository()
    runtime = await create_gitops_discovery_runtime(
        repository=repository,
        custom_objects_api=_CustomObjectsApi([_application()]),
        apps_v1_api=_AppsV1Api(),
        cluster="prod",
    )

    assert any(provider.id == "github-main" and provider.enabled for provider in repository.providers)
    result = await runtime.refresh()
    binding = repository.bindings[("prod", "payments", "Deployment", "api")]

    assert result.coverage.resolved == 1
    assert binding.source is not None
    assert binding.source.provider_id == "github-main"


@pytest.mark.asyncio
async def test_runtime_lazily_uses_the_shared_kubernetes_custom_objects_api(monkeypatch):
    provider = _provider("github-main", api_base_url="https://github.example.test/api/v3")
    repository = _MemoryRepository([provider], {"github-main": ["acme/*"]})
    api = _CustomObjectsApi([_application()])
    api_factory_calls = 0

    async def get_custom_objects_api():
        nonlocal api_factory_calls
        api_factory_calls += 1
        return api

    apps_api_factory_calls = 0

    async def get_apps_v1_api():
        nonlocal apps_api_factory_calls
        apps_api_factory_calls += 1
        return _AppsV1Api()

    monkeypatch.setattr("greenkube.core.k8s_client.get_custom_objects_api", get_custom_objects_api)
    monkeypatch.setattr("greenkube.core.k8s_client.get_apps_v1_api", get_apps_v1_api)
    runtime = await create_gitops_discovery_runtime(repository=repository, cluster="prod")

    result = await runtime.refresh()

    assert api_factory_calls == 1
    assert apps_api_factory_calls == 1
    assert result.coverage.resolved == 1
    assert result.bindings[0].source is not None
    assert result.bindings[0].source.provider_id == "github-main"


@pytest.mark.asyncio
async def test_runtime_refresh_feeds_live_owned_workloads_to_flux_helmrelease_discovery():
    repository = _MemoryRepository()
    custom_objects_api = _CustomObjectsApi(
        responses={
            "helmreleases": {"items": [_helm_release()]},
            "helmrepositories": {"items": [_helm_repository()]},
        }
    )
    apps_api = _AppsV1Api([_helm_workload()])
    runtime = await create_gitops_discovery_runtime(
        repository=repository,
        custom_objects_api=custom_objects_api,
        apps_v1_api=apps_api,
        cluster="prod",
    )

    result = await runtime.refresh()

    binding = repository.bindings[("prod", "apps", "Deployment", "payments")]
    assert len(binding.candidates) == 1
    assert binding.candidates[0].controller_kind is ControllerKind.FLUX_HELM_RELEASE
    assert binding.candidates[0].workload_name == "payments"
    assert result.bindings == (binding,)
    assert [kind for kind, _ in apps_api.calls] == ["Deployment", "StatefulSet", "DaemonSet"]
    assert all(kwargs["limit"] > 0 for _, kwargs in apps_api.calls)


@pytest.mark.asyncio
async def test_runtime_refresh_surfaces_workload_inventory_list_failures():
    error = ApiException(status=403, reason="Forbidden")
    custom_objects_api = _CustomObjectsApi()
    runtime = await create_gitops_discovery_runtime(
        repository=_MemoryRepository(),
        custom_objects_api=custom_objects_api,
        apps_v1_api=_AppsV1Api(failures=[error]),
        cluster="prod",
    )

    with pytest.raises(ApiException) as raised:
        await runtime.refresh()

    assert raised.value is error
    assert custom_objects_api.calls == []


@pytest.mark.asyncio
async def test_periodic_runtime_retries_workload_inventory_list_failures(caplog: pytest.LogCaptureFixture):
    stop_event = asyncio.Event()
    repository = _MemoryRepository()
    custom_objects_api = _CustomObjectsApi(
        responses={
            "helmreleases": {"items": [_helm_release()]},
            "helmrepositories": {"items": [_helm_repository()]},
        }
    )
    apps_api = _AppsV1Api(
        [_helm_workload()],
        failures=[ApiException(status=403, reason="Forbidden")],
        on_daemonsets=stop_event.set,
    )
    runtime = await create_gitops_discovery_runtime(
        repository=repository,
        custom_objects_api=custom_objects_api,
        apps_v1_api=apps_api,
        cluster="prod",
        interval_seconds=1,
    )

    with caplog.at_level(logging.WARNING):
        await runtime.run_periodic(stop_event=stop_event)

    assert sum(kind == "Deployment" for kind, _ in apps_api.calls) == 2
    assert "RBAC" in caplog.text
    binding = repository.bindings[("prod", "apps", "Deployment", "payments")]
    assert len(binding.candidates) == 1
    assert binding.candidates[0].controller_kind is ControllerKind.FLUX_HELM_RELEASE


@pytest.mark.asyncio
async def test_periodic_runtime_preserves_cancellation_while_listing_workloads():
    started = asyncio.Event()

    class _BlockingAppsV1Api:
        async def list_deployment_for_all_namespaces(self, **kwargs: Any) -> object:
            started.set()
            await asyncio.Event().wait()
            return {"items": []}

    runtime = await create_gitops_discovery_runtime(
        repository=_MemoryRepository(),
        apps_v1_api=_BlockingAppsV1Api(),
        cluster="prod",
    )
    task = asyncio.create_task(runtime.run_periodic())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_periodic_discovery_does_not_refresh_without_lease_leadership():
    stop_event = asyncio.Event()
    runtime = GitOpsDiscoveryRuntime(cast(GitOpsDiscoveryService, object()), interval_seconds=1)
    runtime.refresh = AsyncMock()
    task = asyncio.create_task(runtime.run_periodic(stop_event=stop_event, should_run=lambda: False))
    await asyncio.sleep(0)
    stop_event.set()
    await task

    runtime.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_periodic_runtime_logs_kubernetes_errors_and_retries_later(caplog: pytest.LogCaptureFixture):
    stop_event = asyncio.Event()
    failures: list[Exception] = [
        ApiException(status=403, reason="Forbidden"),
        OSError("network unavailable"),
        ApiException(status=503, reason="Service Unavailable"),
    ]

    class _FlakyDiscoveryService:
        async def refresh(self, *, resources: Any = ()) -> DiscoveryRefreshResult:
            if failures:
                raise failures.pop(0)
            stop_event.set()
            return DiscoveryRefreshResult(
                refreshed_at=_NOW,
                bindings=(),
                coverage=DiscoveryCoverage(),
                availability={"argocd": CapabilityAvailability(available=True)},
            )

    runtime = GitOpsDiscoveryRuntime(cast(GitOpsDiscoveryService, _FlakyDiscoveryService()), interval_seconds=1)

    with caplog.at_level(logging.WARNING):
        await runtime.run_periodic(stop_event=stop_event)

    assert not failures
    assert "RBAC" in caplog.text
    assert "network" in caplog.text
    assert "API server" in caplog.text
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_gitops_discovery_refreshes_total{status="succeeded"}' in output
    assert 'greenkube_gitops_discovery_refreshes_total{status="failed"}' in output
    assert 'greenkube_gitops_bindings_total{status="resolved"}' in output
    assert "greenkube_gitops_coverage_ratio" in output
    assert "greenkube_gitops_source_age_seconds" in output


@pytest.mark.asyncio
async def test_discovery_allowlist_globs_do_not_cross_path_segments():
    provider = _ProviderConfig("github-main", api_base_url="https://github.example.test/api/v3")
    repository = _MemoryRepository([provider], {"github-main": ["acme/*"]})
    source = ConfiguredKubernetesDiscoverySource(
        KubernetesDiscoverySource(_CustomObjectsApi([_application("https://github.example.test/acme/team/repo.git")])),
        ConfiguredRepositoryProviderResolver(repository),
    )

    result = await source.discover(cluster="prod", provider_id="unresolved")

    assert len(result.candidates) == 1
    assert result.candidates[0].provider_id == "unresolved"
    assert result.candidates[0].status is BindingStatus.BLOCKED
    assert result.candidates[0].reason == "provider-unresolved"
