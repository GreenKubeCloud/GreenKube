"""Production composition for periodic Kubernetes GitOps discovery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from aiohttp import ClientError
from kubernetes_asyncio.client.exceptions import ApiException

from greenkube.automation.git.base import GitProviderError, parse_repo_url, repository_matches_allowlist
from greenkube.core.leader_election import DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS
from greenkube.core.observability import (
    elapsed,
    record_automation_failure,
    record_gitops_discovery_refresh,
    start_timer,
)
from greenkube.gitops.contracts import (
    DEFAULT_BINDING_TTL,
    UNRESOLVED_PROVIDER_ID,
    BindingStatus,
    WorkloadSource,
)
from greenkube.gitops.discovery.kubernetes import (
    KubernetesDiscoveryResult,
    KubernetesDiscoverySource,
    KubernetesWorkloadInventoryReader,
)
from greenkube.gitops.discovery.service import (
    DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    MAX_DISCOVERY_INTERVAL_SECONDS,
    MIN_DISCOVERY_INTERVAL_SECONDS,
    DiscoveryRefreshResult,
    GitOpsDiscoveryService,
)
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderConfig,
    GitOpsRepository,
    WorkloadBinding,
    WorkloadBindingRepository,
)

logger = logging.getLogger(__name__)

_DISCOVERY_PROVIDER_ID = UNRESOLVED_PROVIDER_ID
_SUPPORTED_PROVIDER_TYPES = frozenset({"github", "gitlab", "gitea"})
_DEFAULT_PROVIDER_HOSTS = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "gitea": "localhost",
}


class KubernetesConfigurationUnavailableError(RuntimeError):
    """Raised when Kubernetes configuration is not yet available to discovery."""


class _GitOpsDiscoveryRepository(Protocol):
    async def list_provider_configs(self, *, enabled_only: bool = False) -> list[GitOpsProviderConfig]: ...

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None: ...

    async def list_provider_allowlists(self, provider_id: str) -> list[str]: ...

    async def save_provider_config(self, provider: GitOpsProviderConfig) -> GitOpsProviderConfig: ...

    async def add_provider_allowlist(self, provider_id: str, repository_pattern: str) -> None: ...

    async def remove_provider_allowlist(self, provider_id: str, repository_pattern: str) -> bool: ...

    async def save_workload_binding(self, binding: WorkloadBinding) -> WorkloadBinding: ...

    async def list_workload_bindings(self, *, cluster: str | None = None) -> list[WorkloadBinding]: ...


class _DiscoverySource(Protocol):
    async def discover(
        self,
        *,
        cluster: str,
        provider_id: str,
        namespace: str | None = None,
        resources: Iterable[Mapping[str, Any]] = (),
    ) -> KubernetesDiscoveryResult: ...


class _WorkloadInventoryReader(Protocol):
    async def list_workloads(self) -> list[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class _ProviderMatcher:
    provider_id: str
    provider_type: str
    host: str
    allowlist: tuple[str, ...]


class ConfiguredRepositoryProviderResolver:
    """Match GitOps source URLs to enabled, allowlisted providers without credentials."""

    def __init__(self, repository: _GitOpsDiscoveryRepository) -> None:
        self._repository = repository

    async def resolve_candidates(self, candidates: Iterable[WorkloadSource]) -> list[WorkloadSource]:
        """Assign unique provider identities, preserving unresolved and ambiguous matches."""
        items = list(candidates)
        if not items:
            return []

        providers = await self._provider_matchers()
        resolved: list[WorkloadSource] = []
        for candidate in items:
            matches = _matching_providers(candidate.repo_url, providers)
            if len(matches) == 1:
                resolved.append(_with_provider(candidate, matches[0].provider_id))
            elif not matches:
                resolved.append(_without_provider_match(candidate))
            else:
                resolved.extend(_with_ambiguous_providers(candidate, matches))
        return resolved

    async def _provider_matchers(self) -> tuple[_ProviderMatcher, ...]:
        configs = await self._repository.list_provider_configs(enabled_only=True)
        providers: list[_ProviderMatcher] = []
        for config in configs:
            if not config.enabled:
                continue
            provider_id = config.id
            provider_type = config.provider_type.strip().lower()
            if not provider_id or provider_id != provider_id.strip() or provider_type not in _SUPPORTED_PROVIDER_TYPES:
                continue
            host = _configured_provider_host(config, provider_type)
            if host is None:
                continue
            allowlist = tuple(
                pattern
                for pattern in await self._repository.list_provider_allowlists(provider_id)
                if isinstance(pattern, str) and pattern and pattern == pattern.strip()
            )
            if allowlist:
                providers.append(_ProviderMatcher(provider_id, provider_type, host, allowlist))
        return tuple(providers)


class ConfiguredKubernetesDiscoverySource:
    """Resolve provider IDs around Kubernetes discovery while preserving CRD availability."""

    def __init__(
        self,
        source: _DiscoverySource,
        provider_resolver: ConfiguredRepositoryProviderResolver,
    ) -> None:
        self._source = source
        self._provider_resolver = provider_resolver

    async def discover(
        self,
        *,
        cluster: str,
        provider_id: str,
        namespace: str | None = None,
        resources: Iterable[Mapping[str, Any]] = (),
    ) -> KubernetesDiscoveryResult:
        discovery = await self._source.discover(
            cluster=cluster,
            provider_id=provider_id,
            namespace=namespace,
            resources=resources,
        )
        candidates = await self._provider_resolver.resolve_candidates(discovery.candidates)
        return KubernetesDiscoveryResult(candidates=candidates, availability=dict(discovery.availability))


class GitOpsDiscoveryRuntime:
    """Run durable binding refreshes with retryable Kubernetes error handling."""

    def __init__(
        self,
        service: GitOpsDiscoveryService,
        *,
        workload_inventory_reader: _WorkloadInventoryReader | None = None,
        interval_seconds: float = DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    ) -> None:
        _validate_interval(interval_seconds)
        self.service = service
        self._workload_inventory_reader = workload_inventory_reader
        self.interval_seconds = float(interval_seconds)

    async def refresh(self) -> DiscoveryRefreshResult:
        """Run one service refresh, including idempotent persistence and stale reconciliation."""
        timer = start_timer()
        result: DiscoveryRefreshResult | None = None
        status = "failed"
        try:
            resources = (
                await self._workload_inventory_reader.list_workloads()
                if self._workload_inventory_reader is not None
                else ()
            )
            result = await self.service.refresh(resources=resources)
            if result is not None:
                status = "succeeded"
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            duration_seconds = elapsed(timer)
            if status != "succeeded":
                record_gitops_discovery_refresh(status=status, duration_seconds=duration_seconds)
                if status == "failed":
                    record_automation_failure("discovery")
            else:
                assert result is not None
                coverage = result.coverage
                record_gitops_discovery_refresh(
                    status="succeeded",
                    duration_seconds=duration_seconds,
                    binding_counts={
                        "resolved": coverage.resolved,
                        "ambiguous": coverage.ambiguous,
                        "unresolved": coverage.unresolved,
                        "blocked": coverage.blocked,
                        "stale": coverage.stale,
                    },
                    coverage_ratio=coverage.ratio,
                    source_age_seconds=_source_age_seconds(result),
                    refreshed_at_timestamp=result.refreshed_at.timestamp(),
                )

    async def run_periodic(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        should_run: Callable[[], bool] | None = None,
    ) -> None:
        """Refresh immediately and retry failures on the configured interval until stopped."""
        stop = stop_event if stop_event is not None else asyncio.Event()
        while not stop.is_set():
            if should_run is None or should_run():
                try:
                    result = await self.refresh()
                except Exception as error:
                    _log_refresh_failure(error, self.interval_seconds)
                else:
                    _log_refresh_success(result)
            else:
                logger.debug("Skipping GitOps discovery refresh while this controller is not the Lease holder.")

            if stop.is_set():
                return
            interval = self.interval_seconds
            if should_run is not None and not should_run():
                interval = min(interval, DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue


async def create_gitops_discovery_runtime(
    *,
    repository: _GitOpsDiscoveryRepository | None = None,
    db_manager: Any | None = None,
    custom_objects_api: Any | None = None,
    apps_v1_api: Any | None = None,
    discovery_source: _DiscoverySource | None = None,
    workload_inventory_reader: _WorkloadInventoryReader | None = None,
    cluster: str | None = None,
    interval_seconds: float | None = None,
) -> GitOpsDiscoveryRuntime:
    """Compose Kubernetes discovery with the configured Git provider and binding stores."""
    config = _get_config() if repository is None or cluster is None or interval_seconds is None else None
    if repository is None:
        if db_manager is None:
            from greenkube.core.db import get_db_manager

            db_manager = get_db_manager()
        assert config is not None
        repository = _make_gitops_repository(db_manager, config.DB_TYPE)

    raw_provider_config = os.getenv("GITOPS_PROVIDERS_JSON")
    if raw_provider_config is not None:
        from greenkube.automation.plan_runtime import parse_gitops_providers_json, reconcile_gitops_providers

        await reconcile_gitops_providers(
            repository,
            parse_gitops_providers_json(raw_provider_config, allow_empty=True),
        )

    await _ensure_unresolved_provider(repository)

    if discovery_source is None:
        selected_api = custom_objects_api if custom_objects_api is not None else _LazyCustomObjectsApi()
        discovery_source = KubernetesDiscoverySource(selected_api)

    if workload_inventory_reader is None:
        if apps_v1_api is not None:
            workload_inventory_reader = KubernetesWorkloadInventoryReader(apps_v1_api)
        else:
            workload_inventory_reader = KubernetesWorkloadInventoryReader(
                _LazyAppsV1Api(),
                autoscaling_v2_api=_LazyAutoscalingV2Api(),
                policy_v1_api=_LazyPolicyV1Api(),
            )

    configured_source = ConfiguredKubernetesDiscoverySource(
        discovery_source,
        ConfiguredRepositoryProviderResolver(repository),
    )
    selected_interval = (
        interval_seconds
        if interval_seconds is not None
        else (config.gitops_discovery_interval_seconds if config is not None else DEFAULT_DISCOVERY_INTERVAL_SECONDS)
    )
    selected_ttl = config.gitops_binding_ttl if config is not None else DEFAULT_BINDING_TTL
    service = GitOpsDiscoveryService(
        configured_source,
        cast(WorkloadBindingRepository, repository),
        cluster=cluster or (config.CLUSTER_NAME if config is not None else "") or "default",
        provider_id=_DISCOVERY_PROVIDER_ID,
        binding_ttl=selected_ttl,
    )
    return GitOpsDiscoveryRuntime(
        service,
        workload_inventory_reader=workload_inventory_reader,
        interval_seconds=selected_interval,
    )


async def _ensure_unresolved_provider(repository: _GitOpsDiscoveryRepository) -> None:
    """Persist the disabled FK target used for sources without an authorized provider."""
    configured = await repository.list_provider_configs()
    unresolved = next((provider for provider in configured if provider.id == _DISCOVERY_PROVIDER_ID), None)
    if unresolved is not None:
        if unresolved.enabled or unresolved.secret_name is not None:
            raise ValueError(f"GitOps provider ID '{_DISCOVERY_PROVIDER_ID}' is reserved for unresolved sources.")
        return
    await repository.save_provider_config(
        GitOpsProviderConfig(
            id=_DISCOVERY_PROVIDER_ID,
            provider_type="gitea",
            enabled=False,
            status="unverified",
        )
    )


class _LazyCustomObjectsApi:
    def __init__(self) -> None:
        self._api: Any | None = None

    async def list_cluster_custom_object(self, **kwargs: Any) -> Any:
        return await (await self._get_api()).list_cluster_custom_object(**kwargs)

    async def list_namespaced_custom_object(self, **kwargs: Any) -> Any:
        return await (await self._get_api()).list_namespaced_custom_object(**kwargs)

    async def _get_api(self) -> Any:
        if self._api is not None:
            return self._api
        from greenkube.core.k8s_client import get_custom_objects_api

        api = await get_custom_objects_api()
        if api is None:
            raise KubernetesConfigurationUnavailableError("Kubernetes API configuration is unavailable.")
        self._api = api
        return api


class _LazyAutoscalingV2Api:
    def __init__(self) -> None:
        self._api: Any | None = None

    async def list_horizontal_pod_autoscaler_for_all_namespaces(self, **kwargs: Any) -> Any:
        api = await self._get_api()
        return await api.list_horizontal_pod_autoscaler_for_all_namespaces(**kwargs)

    async def _get_api(self) -> Any:
        if self._api is not None:
            return self._api
        from greenkube.core.k8s_client import get_autoscaling_v2_api

        api = await get_autoscaling_v2_api()
        if api is None:
            raise KubernetesConfigurationUnavailableError("Kubernetes API configuration is unavailable.")
        self._api = api
        return api


class _LazyPolicyV1Api:
    def __init__(self) -> None:
        self._api: Any | None = None

    async def list_pod_disruption_budget_for_all_namespaces(self, **kwargs: Any) -> Any:
        api = await self._get_api()
        return await api.list_pod_disruption_budget_for_all_namespaces(**kwargs)

    async def _get_api(self) -> Any:
        if self._api is not None:
            return self._api
        from greenkube.core.k8s_client import get_policy_v1_api

        api = await get_policy_v1_api()
        if api is None:
            raise KubernetesConfigurationUnavailableError("Kubernetes API configuration is unavailable.")
        self._api = api
        return api


class _LazyAppsV1Api:
    def __init__(self) -> None:
        self._api: Any | None = None

    async def list_deployment_for_all_namespaces(self, **kwargs: Any) -> Any:
        return await self._list("list_deployment_for_all_namespaces", **kwargs)

    async def list_stateful_set_for_all_namespaces(self, **kwargs: Any) -> Any:
        return await self._list("list_stateful_set_for_all_namespaces", **kwargs)

    async def list_daemon_set_for_all_namespaces(self, **kwargs: Any) -> Any:
        return await self._list("list_daemon_set_for_all_namespaces", **kwargs)

    async def _list(self, method_name: str, **kwargs: Any) -> Any:
        api = await self._get_api()
        return await getattr(api, method_name)(**kwargs)

    async def _get_api(self) -> Any:
        if self._api is not None:
            return self._api
        from greenkube.core.k8s_client import get_apps_v1_api

        api = await get_apps_v1_api()
        if api is None:
            raise KubernetesConfigurationUnavailableError("Kubernetes API configuration is unavailable.")
        self._api = api
        return api


def _matching_providers(repo_url: str, providers: tuple[_ProviderMatcher, ...]) -> list[_ProviderMatcher]:
    host = _repository_host(repo_url)
    if host is None:
        return []
    matches: list[_ProviderMatcher] = []
    for provider in providers:
        if provider.host != host:
            continue
        try:
            owner, name = parse_repo_url(repo_url, provider=provider.provider_type)
        except GitProviderError:
            continue
        repository_name = f"{owner}/{name}"
        if any(repository_matches_allowlist(repository_name, pattern) for pattern in provider.allowlist):
            matches.append(provider)
    return matches


def _configured_provider_host(config: GitOpsProviderConfig, provider_type: str) -> str | None:
    api_base_url = config.api_base_url
    if api_base_url is None or not api_base_url.strip():
        return _DEFAULT_PROVIDER_HOSTS.get(provider_type)
    try:
        parsed = urlsplit(api_base_url)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or host is None or parsed.username or parsed.password:
        return None
    return _normalize_host(host, provider_type)


def _repository_host(repo_url: str) -> str | None:
    if not isinstance(repo_url, str) or not repo_url.strip():
        return None
    value = repo_url.strip()
    if value.startswith("git@") and "://" not in value:
        host = value.partition("@")[2].partition(":")[0]
        return _normalize_host(host)
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        host = parsed.hostname
    except ValueError:
        return None
    return _normalize_host(host) if host is not None else None


def _normalize_host(host: str, provider_type: str | None = None) -> str:
    normalized = host.lower().rstrip(".")
    if provider_type == "github" and normalized == "api.github.com":
        return "github.com"
    return normalized


def _with_provider(candidate: WorkloadSource, provider_id: str) -> WorkloadSource:
    return candidate.model_copy(
        update={
            "id": _provider_candidate_id(candidate, provider_id),
            "provider_id": provider_id,
            "renderer_config": _renderer_config_with_provider(candidate, provider_id),
            "evidence": {**candidate.evidence, "provider_resolution": "matched"},
        }
    )


def _without_provider_match(candidate: WorkloadSource) -> WorkloadSource:
    return candidate.model_copy(
        update={
            "id": _provider_candidate_id(candidate, _DISCOVERY_PROVIDER_ID),
            "provider_id": _DISCOVERY_PROVIDER_ID,
            "status": BindingStatus.BLOCKED,
            "confidence": 0.1,
            "reason": "provider-unresolved",
            "evidence": {**candidate.evidence, "provider_resolution": "unresolved"},
        }
    )


def _with_ambiguous_providers(
    candidate: WorkloadSource,
    providers: list[_ProviderMatcher],
) -> list[WorkloadSource]:
    ambiguous: list[WorkloadSource] = []
    for provider in providers:
        ambiguous.append(
            candidate.model_copy(
                update={
                    "id": _provider_candidate_id(candidate, provider.provider_id),
                    "provider_id": provider.provider_id,
                    "renderer_config": _renderer_config_with_provider(candidate, provider.provider_id),
                    "status": BindingStatus.AMBIGUOUS,
                    "confidence": 0.4,
                    "reason": "provider-unresolved",
                    "evidence": {**candidate.evidence, "provider_resolution": "ambiguous"},
                }
            )
        )
    return ambiguous


def _renderer_config_with_provider(candidate: WorkloadSource, provider_id: str) -> dict[str, Any]:
    renderer_config = dict(candidate.renderer_config)
    for location_key in ("manifest_source", "application_source"):
        location = renderer_config.get(location_key)
        if isinstance(location, Mapping) and location.get("repo_url") == candidate.repo_url:
            renderer_config[location_key] = {**location, "provider_id": provider_id}
    values_from = renderer_config.get("values_from")
    if isinstance(values_from, list):
        renderer_config["values_from"] = [
            {**location, "provider_id": provider_id}
            if isinstance(location, Mapping) and location.get("repo_url") == candidate.repo_url
            else location
            for location in values_from
        ]
    return renderer_config


def _provider_candidate_id(candidate: WorkloadSource, provider_id: str) -> str:
    prefix = candidate.id.rsplit("-", 1)[0]
    digest = hashlib.sha256(f"{candidate.id}\0{provider_id}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _source_age_seconds(result: DiscoveryRefreshResult) -> float:
    observed_at = [
        _as_utc_datetime(candidate.observed_at)
        for binding in result.bindings
        for candidate in binding.candidates
        if isinstance(candidate.observed_at, datetime)
    ]
    if not observed_at:
        return 0.0
    refreshed_at = _as_utc_datetime(result.refreshed_at)
    return max(0.0, (refreshed_at - min(observed_at)).total_seconds())


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _log_refresh_success(result: DiscoveryRefreshResult) -> None:
    coverage = result.coverage
    availability = {
        name: "available" if status.available else status.reason or "unavailable"
        for name, status in result.availability.items()
    }
    logger.info(
        "GitOps discovery refresh completed: total=%d resolved=%d ambiguous=%d unresolved=%d blocked=%d stale=%d "
        "capabilities=%s",
        coverage.total,
        coverage.resolved,
        coverage.ambiguous,
        coverage.unresolved,
        coverage.blocked,
        coverage.stale,
        availability,
    )


def _log_refresh_failure(error: Exception, interval_seconds: float) -> None:
    status = _api_status(error)
    if isinstance(error, ApiException):
        if status in {401, 403}:
            logger.error(
                "GitOps Kubernetes discovery RBAC/authorization error (HTTP %s); retrying in %.0f seconds.",
                status,
                interval_seconds,
            )
        elif status is not None and status >= 500:
            logger.warning(
                "GitOps Kubernetes API server error (HTTP %s); retrying in %.0f seconds.",
                status,
                interval_seconds,
            )
        else:
            logger.warning(
                "GitOps Kubernetes API request failed (HTTP %s); retrying in %.0f seconds.",
                status if status is not None else "unknown",
                interval_seconds,
            )
    elif isinstance(error, KubernetesConfigurationUnavailableError):
        logger.warning(
            "GitOps Kubernetes discovery configuration is unavailable; retrying in %.0f seconds.",
            interval_seconds,
        )
    elif isinstance(error, (ClientError, ConnectionError, OSError, TimeoutError)):
        logger.warning(
            "GitOps Kubernetes discovery network error (%s); retrying in %.0f seconds.",
            type(error).__name__,
            interval_seconds,
        )
    else:
        logger.error(
            "GitOps discovery refresh failed (%s); retrying in %.0f seconds.",
            type(error).__name__,
            interval_seconds,
        )


def _api_status(error: Exception) -> int | None:
    status = getattr(error, "status", None)
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        try:
            return int(status)
        except ValueError:
            return None
    return None


def _validate_interval(interval_seconds: float) -> None:
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or not MIN_DISCOVERY_INTERVAL_SECONDS <= interval_seconds <= MAX_DISCOVERY_INTERVAL_SECONDS
    ):
        raise ValueError(
            "interval_seconds must be finite and between "
            f"{MIN_DISCOVERY_INTERVAL_SECONDS:g} and {MAX_DISCOVERY_INTERVAL_SECONDS:g}"
        )


def _make_gitops_repository(db_manager: Any, db_type: str) -> GitOpsRepository:
    if db_type == "postgres":
        from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository

        return PostgresGitOpsRepository(db_manager)
    if db_type == "sqlite":
        from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository

        return SQLiteGitOpsRepository(db_manager)
    raise ValueError(f"GitOps storage does not support DB_TYPE {db_type!r}.")


def _get_config() -> Any:
    from greenkube.core.config import get_config

    return get_config()
