from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.automation.git.base import (
    GitFile,
    GitProviderCapability,
    GitRepository,
    PullRequestOptions,
)
from greenkube.automation.git.credentials import SecretResolverError
from greenkube.automation.git.k8s_secret_resolver import (
    KubernetesCoreV1SecretResolver,
    MountedSecretFileResolver,
)
from greenkube.automation.git.registry import GitProviderRegistry
from greenkube.automation.plan_runtime import (
    PlanRuntimeConfigurationError,
    ProviderSourceSnapshotResolver,
    RegistryPlanRevisionResolver,
    RuntimePlanService,
    _same_workload_source_version,
    create_plan_runtime,
    initialize_gitops_provider_configs,
    parse_gitops_providers_json,
    parse_pull_request_options_json,
    reconcile_gitops_providers,
)
from greenkube.automation.plan_worker import GitProviderTarget, RegistryPlanProviderResolver
from greenkube.automation.plans import AutomationPlanError, AutomationPlanService, GitBlobFile
from greenkube.automation.rollback import ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY
from greenkube.core.optimization.applied_detector import K8sWorkloadReader
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.planning.raw_manifest import RawManifestPlanner
from greenkube.gitops.renderers.raw import render_raw
from greenkube.gitops.snapshots import RepositorySnapshot, SnapshotLimits
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, WorkloadBinding

TOKEN = "never-log-this-provider-token"
REVISION = "a" * 40


def _snapshot_source() -> WorkloadSource:
    return WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )


def _provider_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    root = f"gitops-{REVISION[:7]}"
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(f"{root}/{path}", content)
    return buffer.getvalue()


class _EmptyPolicyPlanRepository:
    async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None):
        return []

    async def get_plan_record(self, plan_id: str):
        return None


def _provider_json() -> str:
    return json.dumps(
        {
            "providers": [
                {
                    "id": "github-main",
                    "type": "github",
                    "apiBaseUrl": "https://api.github.test",
                    "tokenSecret": {"namespace": "greenkube", "name": "git-provider", "key": "token"},
                    "repositories": ["acme/gitops", "acme/platform"],
                }
            ]
        }
    )


def test_chart_provider_json_accepts_only_the_derived_token_file_path():
    provider = {
        "id": "github-main",
        "type": "github",
        "secretNamespace": "greenkube",
        "secretName": "git-provider",
        "secretKey": "token",
        "tokenFile": "/etc/greenkube/git-providers/greenkube/git-provider/token",
        "repositoryAllowlist": ["acme/gitops"],
    }

    parsed = parse_gitops_providers_json(json.dumps([provider]))

    assert parsed[0].token_file == provider["tokenFile"]
    provider["tokenFile"] = "/etc/greenkube/secrets/token"
    with pytest.raises(PlanRuntimeConfigurationError, match="tokenFile.*git-providers"):
        parse_gitops_providers_json(json.dumps([provider]))


def test_unresolved_discovery_provider_id_is_reserved():
    value = json.loads(_provider_json())
    value["providers"][0]["id"] = "unresolved"

    with pytest.raises(PlanRuntimeConfigurationError, match="reserved"):
        parse_gitops_providers_json(json.dumps(value))


def test_pull_request_options_use_repository_agnostic_defaults_and_validate_the_schema():
    defaults = parse_pull_request_options_json(None)
    assert defaults == PullRequestOptions()
    assert defaults.labels == ()

    configured = parse_pull_request_options_json(
        json.dumps(
            {
                "draft": True,
                "labels": ["release-review"],
                "reviewers": ["123"],
                "teamReviewers": ["platform"],
            }
        )
    )
    assert configured == PullRequestOptions(
        draft=True,
        labels=("release-review",),
        reviewers=("123",),
        team_reviewers=("platform",),
    )

    with pytest.raises(PlanRuntimeConfigurationError, match="GITOPS_PULL_REQUESTS_JSON"):
        parse_pull_request_options_json('{"teamReviewer":["platform"]}')
    with pytest.raises(PlanRuntimeConfigurationError, match="GITOPS_PULL_REQUESTS_JSON"):
        parse_pull_request_options_json('{"draft":"true"}')


class MemoryGitOpsRepository:
    def __init__(self) -> None:
        self.configs: dict[str, GitOpsProviderConfig] = {}
        self.allowlists: dict[str, set[str]] = defaultdict(set)
        self.saved_configs: list[GitOpsProviderConfig] = []
        self.added_allowlists: list[tuple[str, str]] = []
        self.removed_allowlists: list[tuple[str, str]] = []

    async def save_provider_config(self, provider: GitOpsProviderConfig) -> GitOpsProviderConfig:
        self.saved_configs.append(provider)
        self.configs[provider.id] = provider
        return provider

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        return self.configs.get(provider_id)

    async def list_provider_configs(self, *, enabled_only: bool = False) -> list[GitOpsProviderConfig]:
        providers = list(self.configs.values())
        return [provider for provider in providers if provider.enabled or not enabled_only]

    async def add_provider_allowlist(self, provider_id: str, repository_pattern: str) -> None:
        self.added_allowlists.append((provider_id, repository_pattern))
        self.allowlists[provider_id].add(repository_pattern)

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        return sorted(self.allowlists[provider_id])

    async def remove_provider_allowlist(self, provider_id: str, repository_pattern: str) -> bool:
        self.removed_allowlists.append((provider_id, repository_pattern))
        if repository_pattern not in self.allowlists[provider_id]:
            return False
        self.allowlists[provider_id].remove(repository_pattern)
        return True


@pytest.mark.asyncio
async def test_production_composition_shares_typed_repositories_and_authorized_services(monkeypatch):
    import greenkube.automation.plan_runtime as plan_runtime

    class DatabaseManager:
        connected = False

        async def connect(self) -> None:
            self.connected = True

    class SecretReader:
        async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str:
            return TOKEN

    class OperationRepository:
        pass

    class PullRequestRepository:
        pass

    class RecommendationRepository:
        pass

    database = DatabaseManager()
    gitops = MemoryGitOpsRepository()
    operations = OperationRepository()
    pull_requests = PullRequestRepository()
    recommendations = RecommendationRepository()
    monkeypatch.setattr(plan_runtime, "get_config", lambda: SimpleNamespace(DB_TYPE="sqlite", CLUSTER_NAME="prod"))
    monkeypatch.setenv(
        "GITOPS_PULL_REQUESTS_JSON",
        json.dumps(
            {
                "draft": True,
                "labels": ["release-review"],
                "reviewers": ["123"],
                "teamReviewers": ["platform"],
            }
        ),
    )

    runtime = await create_plan_runtime(
        config_json=_provider_json(),
        db_manager=database,
        gitops_repository=cast(Any, gitops),
        plan_operation_repository=cast(Any, operations),
        pull_request_repository=cast(Any, pull_requests),
        recommendation_repository=cast(Any, recommendations),
        secret_resolver=SecretReader(),
    )

    assert database.connected
    assert isinstance(runtime.registry, GitProviderRegistry)
    assert runtime.gitops_repository is gitops
    assert runtime.worker.operations is operations
    assert runtime.worker.plans is gitops
    assert runtime.worker.pull_request_repository is pull_requests
    assert runtime.worker.binding_reader is gitops
    assert runtime.worker.provider_resolver is runtime.provider_resolver
    assert runtime.worker.revision_resolver is runtime.revision_resolver
    assert isinstance(runtime.worker.workload_reader, K8sWorkloadReader)
    assert runtime.worker.pull_request_options == PullRequestOptions(
        draft=True,
        labels=("release-review",),
        reviewers=("123",),
        team_reviewers=("platform",),
    )
    assert runtime.plan_service.worker is runtime.worker
    assert isinstance(runtime.plan_service._plan_service.planner, RawManifestPlanner)
    assert runtime.plan_service._plan_service.operation_enqueuer is operations
    assert gitops.allowlists["github-main"] == {"acme/gitops", "acme/platform"}


def test_provider_json_maps_secret_free_chart_values_to_typed_provider_configs():
    providers = parse_gitops_providers_json(_provider_json())

    assert len(providers) == 1
    assert providers[0].config == GitOpsProviderConfig(
        id="github-main",
        provider_type="github",
        api_base_url="https://api.github.test",
        secret_namespace="greenkube",
        secret_name="git-provider",
        secret_key="token",
    )
    assert providers[0].repositories == ("acme/gitops", "acme/platform")


def test_provider_json_rejects_inline_credentials_without_echoing_the_value():
    value = json.dumps(
        {
            "providers": [
                {
                    "id": "github-main",
                    "type": "github",
                    "token": TOKEN,
                    "repositories": ["acme/gitops"],
                }
            ]
        }
    )

    with pytest.raises(PlanRuntimeConfigurationError) as error:
        parse_gitops_providers_json(value)

    assert TOKEN not in str(error.value)
    assert "Secret" in str(error.value)


@pytest.mark.asyncio
async def test_provider_config_and_allowlists_reconcile_idempotently():
    repository = MemoryGitOpsRepository()
    repository.allowlists["github-main"].update({"acme/gitops", "stale/*"})
    providers = parse_gitops_providers_json(_provider_json())

    await reconcile_gitops_providers(repository, providers)
    write_counts = (len(repository.saved_configs), len(repository.added_allowlists), len(repository.removed_allowlists))

    await reconcile_gitops_providers(repository, providers)

    assert repository.allowlists["github-main"] == {"acme/gitops", "acme/platform"}
    assert repository.removed_allowlists == [("github-main", "stale/*")]
    assert write_counts == (1, 1, 1)
    assert (
        len(repository.saved_configs),
        len(repository.added_allowlists),
        len(repository.removed_allowlists),
    ) == write_counts


@pytest.mark.asyncio
async def test_empty_provider_registry_disables_and_clears_removed_providers():
    repository = MemoryGitOpsRepository()
    configured = parse_gitops_providers_json(_provider_json())[0]
    await repository.save_provider_config(configured.config)
    repository.allowlists[configured.config.id].update(configured.repositories)

    result = await initialize_gitops_provider_configs(
        repository=repository,
        config_json='{"providers":[]}',
    )

    assert result == ()
    assert repository.configs[configured.config.id].enabled is False
    assert repository.allowlists[configured.config.id] == set()


def test_mounted_secret_resolver_names_the_expected_scoped_mount_without_reading_values():
    resolver = MountedSecretFileResolver(Path("configured-git-secrets"))
    provider = GitOpsProviderConfig(
        id="github-main",
        provider_type="github",
        secret_namespace="greenkube",
        secret_name="git-provider",
        secret_key="token",
    )

    assert resolver.path_for_reference("greenkube", "git-provider", "token") == (
        Path.cwd() / "configured-git-secrets/greenkube/git-provider/token"
    )
    default_path = MountedSecretFileResolver().path_for_reference("greenkube", "git-provider", "token")
    assert default_path.is_absolute()
    assert default_path.parts[-4:] == ("git-providers", "greenkube", "git-provider", "token")
    with pytest.raises(SecretResolverError) as error:
        resolver.validate_provider_mount(provider)

    assert "read-only" in str(error.value)
    assert "configured-git-secrets/greenkube/git-provider/token" in str(error.value)


def test_mounted_secret_resolver_uses_the_configured_mount_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    mount_root = tmp_path / "provider-secrets"
    monkeypatch.setenv("GITOPS_SECRET_MOUNT", str(mount_root))

    resolver = MountedSecretFileResolver()

    assert resolver.mount_path == mount_root
    assert resolver.path_for_reference(None, "git-provider", "token") == mount_root / "git-provider/token"


@pytest.mark.asyncio
async def test_mounted_secret_resolver_separates_same_name_and_key_by_namespace(tmp_path):
    resolver = MountedSecretFileResolver(tmp_path)
    secret_a = tmp_path / "namespace-a" / "git-provider" / "token"
    secret_b = tmp_path / "namespace-b" / "git-provider" / "token"
    secret_a.parent.mkdir(parents=True)
    secret_b.parent.mkdir(parents=True)
    secret_a.write_text("namespace-a-token", encoding="utf-8")
    secret_b.write_text("namespace-b-token", encoding="utf-8")

    assert (
        await resolver.resolve_secret(
            namespace="namespace-a",
            name="git-provider",
            key="token",
        )
        == "namespace-a-token"
    )
    assert (
        await resolver.resolve_secret(
            namespace="namespace-b",
            name="git-provider",
            key="token",
        )
        == "namespace-b-token"
    )
    assert resolver.path_for_reference("namespace-a", "git-provider", "token") != (
        resolver.path_for_reference("namespace-b", "git-provider", "token")
    )


@pytest.mark.asyncio
async def test_core_v1_secret_reader_is_injected_and_decodes_only_the_referenced_key():
    class CoreV1Fake:
        calls: list[tuple[str, str]] = []

        async def read_namespaced_secret(self, *, name: str, namespace: str) -> Any:
            self.calls.append((namespace, name))
            return SimpleNamespace(data={"token": base64.b64encode(TOKEN.encode()).decode()})

    core_v1 = CoreV1Fake()
    resolver = KubernetesCoreV1SecretResolver(core_v1, default_namespace="greenkube")

    assert await resolver.resolve_secret(namespace=None, name="git-provider", key="token") == TOKEN
    assert core_v1.calls == [("greenkube", "git-provider")]


@pytest.mark.asyncio
async def test_revision_resolver_uses_authorized_provider_ref_revision_protocol():
    class RevisionProvider:
        name = "github"
        capabilities = frozenset({"multi_file_commit"})
        author_name = "GreenKube Bot"
        author_email = "bot@greenkube.cloud"

        def __init__(self) -> None:
            self.calls: list[tuple[GitRepository, str]] = []
            self.closed = False

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
            self.calls.append((repo, ref))
            return REVISION

        async def close(self) -> None:
            self.closed = True

    class ProviderRepository:
        async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig:
            return GitOpsProviderConfig(
                id=provider_id,
                provider_type="github",
                secret_namespace="greenkube",
                secret_name="git-provider",
                secret_key="token",
            )

    provider = RevisionProvider()

    class Registry:
        async def get_provider(self, provider_id: str, repository: GitRepository) -> RevisionProvider:
            return provider

    source = WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )
    provider_resolver = RegistryPlanProviderResolver(ProviderRepository(), cast(Any, Registry()))
    revision_resolver = RegistryPlanRevisionResolver(provider_resolver)

    assert await revision_resolver.resolve_current_revision(source) == REVISION
    assert provider.calls == [
        (GitRepository(provider="github", owner="acme", name="gitops", default_branch="main"), "main")
    ]
    assert provider.closed


@pytest.mark.asyncio
async def test_source_snapshot_resolver_uses_bounded_revision_pinned_provider_archive():
    source = _snapshot_source()
    repository = GitRepository(provider="github", owner="acme", name="gitops", default_branch="main")
    files = {
        "apps/payments/deployment.yaml": b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n",
        "README.md": b"repository documentation",
    }
    archive = _provider_archive(files)

    class Provider:
        name = "github"
        capabilities = frozenset(
            {
                GitProviderCapability.GET_REF_REVISION,
                GitProviderCapability.REPOSITORY_ARCHIVE,
            }
        )

        def __init__(self) -> None:
            self.closed = False
            self.ref_calls: list[tuple[GitRepository, str]] = []
            self.archive_calls: list[tuple[GitRepository, str, int]] = []

        def supports(self, capability: GitProviderCapability | str) -> bool:
            return capability in self.capabilities

        async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
            assert (repo, ref) == (repository, "main")
            self.ref_calls.append((repo, ref))
            return REVISION

        async def list_files(self, repo: GitRepository, ref: str) -> list[str]:
            raise AssertionError("archive snapshots must not list repository files")

        async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile:
            raise AssertionError("archive snapshots must not make per-file reads")

        async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
            self.archive_calls.append((repo, revision, max_bytes))
            return archive

        async def close(self) -> None:
            self.closed = True

    provider = Provider()

    class AuthorizedProviderResolver:
        async def resolve(self, selected_source: WorkloadSource) -> GitProviderTarget:
            assert selected_source == source
            return GitProviderTarget(provider=cast(Any, provider), repository=repository)

    resolver = ProviderSourceSnapshotResolver(cast(Any, AuthorizedProviderResolver()))

    snapshot = await resolver.create_snapshot(source)
    with snapshot:
        assert snapshot.metadata.provider_id == source.provider_id
        assert snapshot.metadata.repo_url == source.repo_url
        assert snapshot.metadata.commit_sha == REVISION
        assert snapshot.metadata.source_kind == "archive"
        assert snapshot.metadata.compressed_bytes == len(archive)
        assert {item.path for item in snapshot.files} == set(files)
        assert (snapshot.workspace / "apps/payments/deployment.yaml").read_bytes() == files[
            "apps/payments/deployment.yaml"
        ]

    assert provider.closed
    assert provider.ref_calls == [(repository, source.ref)]
    assert provider.archive_calls == [(repository, REVISION, SnapshotLimits().max_compressed_bytes)]


@pytest.mark.asyncio
async def test_source_snapshot_resolver_fails_closed_when_archive_capability_is_missing():
    source = _snapshot_source()
    repository = GitRepository(provider="github", owner="acme", name="gitops", default_branch="main")

    class Provider:
        name = "github"

        def __init__(self) -> None:
            self.closed = False

        def supports(self, capability: GitProviderCapability | str) -> bool:
            return False

        async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
            return REVISION

        async def list_files(self, repo: GitRepository, ref: str) -> list[str]:
            raise AssertionError("unsupported archive behavior must not fall back to file listings")

        async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile:
            raise AssertionError("unsupported archive behavior must not fall back to per-file reads")

        async def close(self) -> None:
            self.closed = True

    provider = Provider()

    class AuthorizedProviderResolver:
        async def resolve(self, selected_source: WorkloadSource) -> GitProviderTarget:
            return GitProviderTarget(provider=cast(Any, provider), repository=repository)

    resolver = ProviderSourceSnapshotResolver(cast(Any, AuthorizedProviderResolver()))

    with pytest.raises(AutomationPlanError) as error:
        await resolver.create_snapshot(source)

    assert error.value.code is AutomationErrorCode.PROVIDER_NOT_READY
    assert provider.closed


@pytest.mark.asyncio
async def test_source_snapshot_resolver_rejects_a_mismatched_authorized_repository():
    source = _snapshot_source()
    repository = GitRepository(provider="github", owner="other", name="gitops", default_branch="main")

    class Provider:
        name = "github"

        def __init__(self) -> None:
            self.closed = False

        def supports(self, capability: GitProviderCapability | str) -> bool:
            return True

        async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
            raise AssertionError("repository identity must be checked before resolving a revision")

        async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
            raise AssertionError("repository identity must be checked before requesting an archive")

        async def close(self) -> None:
            self.closed = True

    provider = Provider()

    class AuthorizedProviderResolver:
        async def resolve(self, selected_source: WorkloadSource) -> GitProviderTarget:
            return GitProviderTarget(provider=cast(Any, provider), repository=repository)

    resolver = ProviderSourceSnapshotResolver(cast(Any, AuthorizedProviderResolver()))

    with pytest.raises(AutomationPlanError) as error:
        await resolver.create_snapshot(source)

    assert error.value.code is AutomationErrorCode.SOURCE_CHANGED
    assert provider.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["compressed", "extracted", "file_count", "per_file"])
async def test_source_snapshot_resolver_preserves_all_archive_snapshot_limits(limit_kind):
    source = _snapshot_source()
    repository = GitRepository(provider="github", owner="acme", name="gitops", default_branch="main")
    archive = _provider_archive(
        {
            "apps/payments/deployment.yaml": b"12",
            "README.md": b"x",
        }
    )
    limits_by_kind = {
        "compressed": SnapshotLimits(
            max_compressed_bytes=len(archive) - 1,
            max_extracted_bytes=100,
            max_files=10,
            max_file_bytes=100,
        ),
        "extracted": SnapshotLimits(max_compressed_bytes=1000, max_extracted_bytes=2, max_files=10, max_file_bytes=10),
        "file_count": SnapshotLimits(
            max_compressed_bytes=1000,
            max_extracted_bytes=100,
            max_files=1,
            max_file_bytes=10,
        ),
        "per_file": SnapshotLimits(max_compressed_bytes=1000, max_extracted_bytes=100, max_files=10, max_file_bytes=1),
    }

    class Provider:
        name = "github"
        capabilities = frozenset({GitProviderCapability.REPOSITORY_ARCHIVE})

        def __init__(self) -> None:
            self.closed = False
            self.archive_limit: int | None = None

        def supports(self, capability: GitProviderCapability | str) -> bool:
            return capability in self.capabilities

        async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
            return REVISION

        async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
            self.archive_limit = max_bytes
            return archive

        async def close(self) -> None:
            self.closed = True

    provider = Provider()

    class AuthorizedProviderResolver:
        async def resolve(self, selected_source: WorkloadSource) -> GitProviderTarget:
            return GitProviderTarget(provider=cast(Any, provider), repository=repository)

    resolver = ProviderSourceSnapshotResolver(
        cast(Any, AuthorizedProviderResolver()),
        limits=limits_by_kind[limit_kind],
    )

    with pytest.raises(PlanRuntimeConfigurationError):
        await resolver.create_snapshot(source)

    assert provider.archive_limit == limits_by_kind[limit_kind].max_compressed_bytes
    assert provider.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("renderer", [RendererKind.RAW, RendererKind.KUSTOMIZE])
async def test_api_plan_service_uses_only_persisted_recommendation_and_resolved_binding(renderer):
    action = ActionPlanV3(
        action_id="action-1",
        recommendation_ids=[41],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation/41",
    )
    recommendation = RecommendationRecord(
        id=41,
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        scope="workload",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU requests.",
        action=action,
        status=RecommendationStatus.ACTIVE,
        risk_level="low",
        confidence=0.9,
        evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
    )
    source = WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=renderer,
        renderer_config=(
            {"values_files": ["apps/payments/values.yaml"]} if renderer is RendererKind.HELM_VALUES else {}
        ),
        confidence=1,
        status=BindingStatus.RESOLVED,
    )

    class Recommendations:
        async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
            return recommendation if rec_id == 41 else None

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            assert (cluster, namespace, workload_kind, workload_name) == (
                "prod",
                "payments",
                "Deployment",
                "api",
            )
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=source,
                generation=4,
            )

    class SnapshotResolver:
        def __init__(self) -> None:
            self.sources: list[WorkloadSource] = []
            self.snapshots: list[Any] = []

        async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot:
            self.sources.append(source)

            class Snapshot:
                files = (SimpleNamespace(path="apps/payments/values.yaml"),)

                def close(self) -> None:
                    self.closed = True

            snapshot = Snapshot()
            self.snapshots.append(snapshot)
            return cast(RepositorySnapshot, snapshot)

    plan_sentinel = object()

    class PlanService:
        def __init__(self) -> None:
            self.sources: list[WorkloadSource] = []
            self.error: AutomationPlanError | None = None

        async def create_plan(self, action, source, snapshot, **kwargs):
            assert action is not None and source is not None and snapshot is not None
            self.sources.append(source)
            if self.error is not None:
                raise self.error
            return plan_sentinel

    snapshot_resolver = SnapshotResolver()
    plan_service = PlanService()
    service = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=Bindings(),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=cast(Any, plan_service),
        policy_repository=cast(Any, _EmptyPolicyPlanRepository()),
    )

    resolved_action, resolved_source = await service._resolve_request([41])

    assert resolved_action == action
    assert resolved_source.id == source.id
    assert resolved_source.evidence["binding_generation"] == 4
    mapping = {
        "file_path": "apps/payments/values.yaml",
        "container": "api",
        "path": ["app", "resources"],
    }
    assert (
        await service.create_plan(
            [41],
            values_mapping=mapping if renderer is RendererKind.HELM_VALUES else None,
        )
        is plan_sentinel
    )
    if renderer is RendererKind.HELM_VALUES:
        planned_source = plan_service.sources[-1]
        assert planned_source.renderer_config["values_mapping"] == mapping
        assert planned_source.evidence[ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY] is True
        assert "values_mapping" not in source.renderer_config
        current_source = source.model_copy(update={"evidence": {"binding_generation": 4}})
        assert _same_workload_source_version(planned_source, current_source)
        assert not _same_workload_source_version(
            planned_source,
            current_source.model_copy(update={"evidence": {"binding_generation": 5}}),
        )
        assert not _same_workload_source_version(
            planned_source,
            current_source.model_copy(update={"renderer_config": {"values_files": ["apps/payments/other.yaml"]}}),
        )
        with pytest.raises(AutomationPlanError) as invalid_file:
            await service.create_plan(
                [41],
                values_mapping={**mapping, "file_path": "apps/payments/other.yaml"},
            )
        assert invalid_file.value.code is AutomationErrorCode.UNSUPPORTED_SOURCE
        with pytest.raises(AutomationPlanError) as forged_identity:
            await service.create_plan([41], values_mapping={**mapping, "repo_url": "https://attacker.invalid/repo"})
        assert forged_identity.value.code is AutomationErrorCode.UNSUPPORTED_SOURCE
        assert len(snapshot_resolver.snapshots) == 1

        plan_service.error = AutomationPlanError(
            AutomationErrorCode.NEEDS_CHART_MAPPING,
            "mapping needed",
        )
        with pytest.raises(AutomationPlanError) as error:
            await service.create_plan([41])
        assert error.value.code is AutomationErrorCode.NEEDS_CHART_MAPPING
        assert error.value.mapping_options == {
            "files": ["apps/payments/values.yaml"],
            "containers": ["api"],
        }
        assert snapshot_resolver.snapshots[-1].closed
    with pytest.raises(TypeError):
        await service.create_plan([41], source=source)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_single_requested_recommendation_resolves_full_persisted_group():
    action = ActionPlanV3(
        action_id="grouped-action",
        recommendation_ids=[41, 42],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m", "memory": "256Mi"}),
            )
        ],
        evidence_ref="recommendation/41/42",
    )
    records = {
        recommendation_id: RecommendationRecord(
            id=recommendation_id,
            namespace="payments",
            owner_kind="Deployment",
            owner_name="api",
            scope="workload",
            type=rec_type,
            description="Reduce workload requests.",
            action=action,
            status=RecommendationStatus.ACTIVE,
        )
        for recommendation_id, rec_type in (
            (41, RecommendationType.RIGHTSIZING_CPU),
            (42, RecommendationType.RIGHTSIZING_MEMORY),
        )
    }
    records[99] = records[41].model_copy(
        update={
            "id": 99,
            "action": action.model_copy(update={"action_id": "unrelated-action", "recommendation_ids": [99]}),
        }
    )
    source = WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )

    class Recommendations:
        calls: list[int] = []

        async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
            self.calls.append(rec_id)
            return records.get(rec_id)

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=source,
            )

    recommendation_repository = Recommendations()
    service = RuntimePlanService(
        recommendation_repository=cast(Any, recommendation_repository),
        binding_repository=Bindings(),
        cluster="prod",
        snapshot_resolver=cast(Any, object()),
        plan_service=AutomationPlanService(object()),  # type: ignore[arg-type]
    )

    resolved_action, resolved_source = await service._resolve_request([41])

    assert resolved_action is action
    assert resolved_action.recommendation_ids == [41, 42]
    assert recommendation_repository.calls == [41, 42]
    assert resolved_source is source
    with pytest.raises(AutomationPlanError, match="persisted actionable plan"):
        await service._resolve_request([41, 99])


@pytest.mark.asyncio
async def test_api_plan_service_snapshots_helm_sources_and_dispatches_to_plan_service():
    action = ActionPlanV3(
        action_id="action-1",
        recommendation_ids=[41],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation/41",
    )
    recommendation = RecommendationRecord(
        id=41,
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        scope="workload",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU requests.",
        action=action,
        status=RecommendationStatus.ACTIVE,
        risk_level="low",
        confidence=0.9,
        evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
    )
    source = WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.HELM_VALUES,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )

    class Recommendations:
        async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
            return recommendation if rec_id == 41 else None

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=source,
            )

    class SnapshotResolver:
        called = False

        async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot:
            self.called = True

            class Snapshot:
                def close(self) -> None:
                    pass

            return cast(RepositorySnapshot, Snapshot())

    snapshot_resolver = SnapshotResolver()
    plan_sentinel = object()

    class PlanService:
        called = False

        async def create_plan(self, action, source, snapshot, **kwargs):
            self.called = True
            assert action is recommendation.action
            assert source.renderer is RendererKind.HELM_VALUES
            assert snapshot is not None
            return plan_sentinel

    plan_service = PlanService()
    service = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=Bindings(),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=cast(Any, plan_service),
        policy_repository=cast(Any, _EmptyPolicyPlanRepository()),
    )

    result = await service.prepare_plan([41])

    assert result is plan_sentinel
    assert snapshot_resolver.called
    assert plan_service.called


@pytest.mark.asyncio
async def test_runtime_plan_service_forwards_transition_audit_metadata():
    calls: list[tuple[str, ...]] = []
    approved = cast(Plan, object())

    class PlanService:
        async def read_plan(self, plan_id: str) -> Plan | None:
            return None

        async def approve_plan(self, plan_id: str, digest: str, *, actor: str) -> Plan:
            calls.append(("approve", plan_id, digest, actor))
            return approved

        async def mark_stale(self, plan_id: str, *, actor: str, reason_code: str | None) -> Plan | None:
            calls.append(("stale", plan_id, actor, reason_code or ""))
            return None

    service = RuntimePlanService(
        recommendation_repository=cast(Any, object()),
        binding_repository=cast(Any, object()),
        cluster="prod",
        snapshot_resolver=cast(Any, object()),
        plan_service=cast(Any, PlanService()),
    )

    result = await service.approve_plan("plan-1", "digest-1", actor="api-key:abc123")
    stale = await service.mark_stale("plan-2", actor="api-key:def456", reason_code="cancelled_by_user")

    assert result is approved
    assert stale is None
    assert calls == [
        ("approve", "plan-1", "digest-1", "api-key:abc123"),
        ("stale", "plan-2", "api-key:def456", "cancelled_by_user"),
    ]


@pytest.mark.asyncio
async def test_kustomize_rollback_runtime_uses_the_explicit_exact_adapter():
    original_revision = "a" * 40
    current_revision = "b" * 40
    patch_path = "apps/payments/patch.yaml"
    manifest_path = "apps/payments/deployment.yaml"
    base_content = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            requests:
              cpu: 100m
"""
    forward_content = base_content.replace("cpu: 100m", "cpu: 300m")
    digest = f"sha256:{'c' * 64}"
    target = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api")
    action = ActionPlanV3(
        action_id="kustomize-rollback-action",
        recommendation_ids=[41],
        target=target,
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation/41",
    )
    source = WorkloadSource(
        id="source-kustomize-rollback",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        revision=original_revision,
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        source_revision=original_revision,
        renderer=RendererKind.KUSTOMIZE,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )
    original_plan = Plan(
        plan_id="kustomize-original-plan",
        recommendation_ids=[41],
        source=source,
        action=action,
        file_plan=FilePlan(
            plan_id="kustomize-original-plan",
            files=[FileEdit(path=patch_path, operation="create", content="patch\n")],
            diff="forward diff",
            rendered_diff="forward rendered diff",
            base_revision=original_revision,
            source_revision=original_revision,
            renderer=RendererKind.KUSTOMIZE,
            validation=ValidationResult(
                target_changed=True,
                unrelated_changes=False,
                requests_within_limits=True,
                renderer=RendererKind.KUSTOMIZE,
                output_digest=f"sha256:{'d' * 64}",
            ),
            digest=digest,
        ),
        status=PlanStatus.SYNCED,
        digest=digest,
    )
    recommendation = RecommendationRecord(
        id=41,
        container_name="api",
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU requests.",
        scope="workload",
        status=RecommendationStatus.ROLLBACK_REVIEW,
        action=action,
        risk_level="low",
        confidence=0.9,
        evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
    )

    class Recommendations:
        async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
            return recommendation if rec_id == 41 else None

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=source,
            )

    class Snapshot(dict):
        def __init__(self):
            super().__init__({patch_path: "patch\n"})
            self.metadata = SimpleNamespace(
                provider_id=source.provider_id,
                repo_url=source.repo_url,
                commit_sha=current_revision,
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    snapshot = Snapshot()

    class SnapshotResolver:
        calls = 0

        async def create_snapshot(self, resolved_source: WorkloadSource):
            assert resolved_source.id == source.id
            self.calls += 1
            return snapshot

    class KustomizeAdapter:
        renderer = RendererKind.KUSTOMIZE
        supports_exact_target_rollback = True

        def __init__(self):
            self.candidates: list[dict[str, str | bytes]] = []

        def render(self, files: Mapping[str, str | bytes], source: WorkloadSource):
            del source
            self.candidates.append(dict(files))
            manifest = forward_content if patch_path in files else base_content
            return replace(render_raw({manifest_path: manifest}), renderer=RendererKind.KUSTOMIZE)

    class BlobResolver:
        async def get_blob_file(self, source: WorkloadSource, path: str, revision: str):
            raise AssertionError("create-only rollback must not fetch an original file blob")

    class PlanService:
        git_blob_resolver = BlobResolver()

        def __init__(self):
            self.inverse = None
            self.source: WorkloadSource | None = None

        def clock(self):
            return datetime.now(timezone.utc)

        async def read_plan(self, plan_id: str):
            return original_plan if plan_id == original_plan.plan_id else None

        async def create_inverse_plan(self, original, rollback_source, inverse, *, policy_result=None):
            self.inverse = inverse
            self.source = rollback_source
            return inverse

    adapter = KustomizeAdapter()
    plan_service = PlanService()
    snapshot_resolver = SnapshotResolver()
    unconfigured_service = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=Bindings(),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=cast(Any, plan_service),
    )
    with pytest.raises(AutomationPlanError) as error:
        await unconfigured_service.prepare_rollback(original_plan.plan_id)
    assert error.value.code is AutomationErrorCode.ROLLBACK_UNSUPPORTED
    assert snapshot_resolver.calls == 0

    service = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=Bindings(),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=cast(Any, plan_service),
        rollback_renderer=cast(Any, adapter),
        policy_repository=cast(Any, _EmptyPolicyPlanRepository()),
    )

    result = await service.prepare_rollback(original_plan.plan_id)

    assert result is plan_service.inverse
    assert result.renderer is RendererKind.KUSTOMIZE
    assert [patch_path in candidate for candidate in adapter.candidates] == [True, False]
    assert plan_service.source is not None
    assert plan_service.source.revision == current_revision
    assert snapshot.closed


@pytest.mark.asyncio
async def test_rollback_preview_fetches_original_blobs_and_uses_exact_digest_approval():
    original_revision = "a" * 40
    current_revision = "b" * 40
    path = "apps/payments/deployment.yaml"
    original_content = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            requests:
              cpu: 100m
"""
    forward_content = original_content.replace("cpu: 100m", "cpu: 300m")
    original_bytes = original_content.encode()
    original_blob_sha = hashlib.sha1(b"blob " + str(len(original_bytes)).encode() + b"\0" + original_bytes).hexdigest()
    digest = f"sha256:{'c' * 64}"
    target = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api")
    action = ActionPlanV3(
        action_id="original-action",
        recommendation_ids=[41],
        target=target,
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation/41",
    )
    source = WorkloadSource(
        id="source-rollback",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        revision=original_revision,
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        source_revision=original_revision,
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )
    original_file_plan = FilePlan(
        plan_id="original-plan",
        files=[
            FileEdit(
                path=path,
                operation="update",
                original_sha=original_blob_sha,
                content=forward_content,
            )
        ],
        diff="forward diff",
        rendered_diff="forward rendered diff",
        base_revision=original_revision,
        source_revision=original_revision,
        renderer=RendererKind.RAW,
        validation=ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest=f"sha256:{'d' * 64}",
        ),
        digest=digest,
    )
    original_plan = Plan(
        plan_id="original-plan",
        recommendation_ids=[41],
        source=source,
        action=action,
        file_plan=original_file_plan,
        status=PlanStatus.SYNCED,
        digest=digest,
    )
    expected_source_id = source.id
    recommendation = RecommendationRecord(
        id=41,
        container_name="api",
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU requests.",
        scope="workload",
        status=RecommendationStatus.ROLLBACK_REVIEW,
        current_cpu_request_millicores=100,
        recommended_cpu_request_millicores=300,
        action=action,
        risk_level="low",
        confidence=0.9,
        evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
    )

    class Recommendations:
        async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
            return recommendation if rec_id == 41 else None

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=source,
            )

    class Snapshot(dict):
        def __init__(self):
            super().__init__({path: forward_content})
            self.metadata = SimpleNamespace(
                provider_id=source.provider_id,
                repo_url=source.repo_url,
                commit_sha=current_revision,
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    snapshot = Snapshot()

    class SnapshotResolver:
        calls = 0

        async def create_snapshot(self, resolved_source: WorkloadSource):
            assert resolved_source.id == source.id
            self.calls += 1
            return snapshot

    class BlobResolver:
        calls: list[tuple[str, str, str]] = []

        async def get_blob_file(
            self,
            source: WorkloadSource,
            path: str,
            revision: str,
        ) -> GitBlobFile | None:
            self.calls.append((source.id, path, revision))
            return GitBlobFile(path, original_content, original_blob_sha)

    class PlanRepository:
        def __init__(self):
            self.plans = {original_plan.plan_id: original_plan}

        async def get_plan(self, plan_id: str):
            return self.plans.get(plan_id)

        async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None):
            return [
                plan
                for plan in self.plans.values()
                if (status is None or plan.status is status) and (source_id is None or plan.source.id == source_id)
            ]

        async def save_plan(
            self,
            plan: Plan,
            *,
            policy_result: dict[str, Any] | None = None,
            actor: str = "system",
        ) -> Plan:
            self.plans[plan.plan_id] = plan
            return plan

        async def save_plan_if_absent(
            self,
            plan: Plan,
            *,
            policy_result: dict[str, Any] | None = None,
            actor: str = "system",
        ) -> Plan:
            existing = self.plans.get(plan.plan_id)
            if existing is not None:
                return existing
            return await self.save_plan(plan, policy_result=policy_result)

        async def set_plan_status(
            self,
            plan_id: str,
            status: PlanStatus,
            *,
            expected_statuses: tuple[PlanStatus, ...] | None = None,
            expected_digest: str | None = None,
            actor: str = "system",
            reason_code: str | None = None,
        ) -> Plan | None:
            plan = self.plans.get(plan_id)
            if plan is None:
                return None
            updated = plan.model_copy(update={"status": status})
            self.plans[plan_id] = updated
            return updated

        async def set_plan_policy_result(
            self,
            plan_id: str,
            policy_result: dict[str, Any],
            *,
            expected_digest: str,
            expected_statuses=None,
            actor: str = "system",
        ) -> Plan | None:
            plan = self.plans.get(plan_id)
            if (
                plan is None
                or plan.digest != expected_digest
                or (expected_statuses is not None and plan.status not in expected_statuses)
            ):
                return None
            updated = plan.model_copy(update={"policy_result": policy_result})
            self.plans[plan_id] = updated
            return updated

    class RevisionResolver:
        async def resolve_current_revision(self, source: WorkloadSource) -> str | None:
            assert source.id == expected_source_id
            return current_revision

    class OperationQueue:
        def __init__(self):
            self.keys: list[str] = []

        async def enqueue(self, plan: Plan, *, idempotency_key: str):
            self.keys.append(idempotency_key)

    repository = PlanRepository()
    blobs = BlobResolver()
    queue = OperationQueue()
    plan_service = AutomationPlanService(
        cast(Any, repository),
        git_blob_resolver=blobs,
        revision_resolver=cast(Any, RevisionResolver()),
        operation_enqueuer=queue,
    )
    snapshot_resolver = SnapshotResolver()
    service = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=Bindings(),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=plan_service,
        rollback_renderer=cast(
            Any,
            SimpleNamespace(
                renderer=RendererKind.KUSTOMIZE,
                supports_exact_target_rollback=True,
            ),
        ),
    )

    rollback_plan = await service.prepare_rollback(original_plan.plan_id)

    assert rollback_plan.rollback_of_plan_id == original_plan.plan_id
    assert rollback_plan.status is PlanStatus.VALIDATED
    assert rollback_plan.file_plan is not None
    assert rollback_plan.file_plan.base_revision == current_revision
    assert rollback_plan.file_plan.files[0].content == original_content
    assert blobs.calls == [(source.id, path, original_revision)]
    assert snapshot.closed
    assert queue.keys == []

    assert rollback_plan.digest is not None
    approved = await service.approve_plan(rollback_plan.plan_id, rollback_plan.digest)

    assert approved.status is PlanStatus.APPROVED
    assert queue.keys == [f"plan:{rollback_plan.plan_id}:{rollback_plan.digest}"]
    assert recommendation.status is RecommendationStatus.ROLLBACK_REVIEW
    recommendation = recommendation.model_copy(update={"status": RecommendationStatus.ACTIVE})
    with pytest.raises(AutomationPlanError) as error:
        await service.prepare_rollback(original_plan.plan_id)
    assert error.value.code is AutomationErrorCode.ROLLBACK_NOT_READY
    assert blobs.calls == [(source.id, path, original_revision)]
    assert snapshot_resolver.calls == 1
