"""Always-on automation: the ``AutomationReadiness`` reconciler (roadmap F12, F2).

Automation has no feature flag. Every capability activates itself when its
prerequisites are present and stays dormant, with a reason, when they are not:

==============  ===============================================================
Capability      Active when
==============  ===============================================================
discovery       Argo CD or Flux CRDs are present (re-probed every discovery run)
preview         per repository: a provider serves its host, it is allowlisted,
                credentials are mounted and a workload binding resolved
delivery        a repository can be previewed and ``pauseDelivery`` is false
sync_tracking   a provider has credentials (PR polling) or webhooks are set
webhooks        a provider webhook Secret is configured
measurement     always (applied changes are detected from the live state)
renderer        the isolated renderer answers; a warning when its egress-deny
                NetworkPolicy is not enforced (``renderer_egress_unenforced``)
==============  ===============================================================

The reconciler runs in the controller every 60 seconds (``automation-readiness``
loop). Each tick:

1. reads the repositories referenced by discovered Argo CD/Flux sources;
2. infers a Git provider for every repository host that no explicit
   ``automation.providers`` entry serves: ``github.com`` and ``gitlab.com`` by
   name, self-hosted hosts by probing ``/api/v4/version`` (GitLab) and
   ``/api/v1/version`` (Gitea);
3. binds the default credentials Secret (``greenkube-git-credentials``, key
   ``token``) to exactly one inferred host and enables that provider only while the
   Secret is mounted, so creating or deleting it activates or deactivates delivery
   without a restart (the kubelet refreshes the optional projected volume). The
   token goes to the host named by the Secret's optional ``host`` key, or, without
   that key, to github.com or gitlab.com when that is the only inferred host. It
   is never sent to a probe-inferred self-hosted host the key does not name, so a
   GitOps source pointing at a host someone else controls cannot receive it
   (``credentials_host_ambiguous`` / ``credentials_host_mismatch``);
4. expands the ``@discovered`` allowlist into the discovered repositories (plus the
   repositories an administrator allowed, F2), then asks discovery to re-resolve
   bindings when a provider changed;
5. persists per-repository and global capability states
   (``automation_capabilities``, read by ``GET /api/v1/setup/status``) and exports
   ``greenkube_capability_active{capability}``.

The plan executor and the PR-tracking steps of the automation controller loop read
:attr:`AutomationReadiness.delivery_active` and
:attr:`AutomationReadiness.tracking_active` on every tick.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from prometheus_client import Gauge

from greenkube.core.config import DISCOVERED_REPOSITORIES
from greenkube.core.observability import REGISTRY
from greenkube.gitops.contracts import BindingStatus
from greenkube.storage.automation_capability_repository import (
    SCOPE_GLOBAL,
    SCOPE_REPOSITORY,
    AllowedRepository,
    CapabilityRow,
)
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, WorkloadBinding

logger = logging.getLogger(__name__)

READINESS_INTERVAL_SECONDS = 60.0
INFERRED_PROVIDER_PREFIX = "inferred-"
UNRESOLVED_PROVIDER_ID = "unresolved"
DEFAULT_CREDENTIALS_KEY = "token"
#: Optional keys of the default Secret naming the one host its token belongs to.
DEFAULT_CREDENTIALS_HOST_KEYS = ("host", "apiBaseUrl")
CREDENTIALS_HOST_AMBIGUOUS = "credentials_host_ambiguous"
CREDENTIALS_HOST_MISMATCH = "credentials_host_mismatch"
_MAX_HOST_VALUE_BYTES = 512
#: A failed self-hosted probe is retried after this delay; a success is kept.
PROBE_RETRY_SECONDS = 600.0
PROBE_TIMEOUT_SECONDS = 5.0

STATE_ACTIVE = "active"
STATE_DORMANT = "dormant"
STATE_PENDING = "pending"

CAPABILITIES = ("discovery", "preview", "delivery", "sync_tracking", "webhooks", "measurement", "renderer")

#: Discovery capability keys of the GitOps controllers themselves (not their sources).
_CONTROLLER_CAPABILITIES = ("argocd", "flux_kustomization", "flux_helm_release")
_WELL_KNOWN_HOSTS: Mapping[str, str] = {"github.com": "github", "gitlab.com": "gitlab"}
_PROVIDER_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

CAPABILITY_ACTIVE = Gauge(
    "greenkube_capability_active",
    "1 when an automation capability is active, 0 when dormant (roadmap F12).",
    ["capability"],
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- inference


@dataclass(frozen=True)
class ProviderInference:
    """Provider type and API base URL inferred for one repository host (F2)."""

    provider_type: str
    api_base_url: str | None
    inferred_from: str  # "host" (well-known) or "probe" (self-hosted)


class HostProber(Protocol):
    async def __call__(self, base_url: str) -> ProviderInference | None: ...


@dataclass(frozen=True)
class RepositoryRef:
    """A discovered repository: URL, host, browser base URL and ``owner/name``."""

    repo_url: str
    host: str
    base_url: str
    repository: str


def repository_ref(repo_url: str) -> RepositoryRef | None:
    """Parse an HTTPS or SSH Git remote into its host and ``owner/name`` (nested groups kept)."""
    value = (repo_url or "").strip()
    if not value or any(character in value for character in "\r\n?#"):
        return None
    if value.startswith("git@") and "://" not in value:
        host_part, _, path = value.partition("@")[2].partition(":")
        host, scheme, port = host_part, "https", None
    else:
        try:
            parsed = urlsplit(value if "://" in value else f"https://{value}")
            host, port, path = parsed.hostname, parsed.port, parsed.path
        except ValueError:
            return None
        scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    if not host:
        return None
    host = host.lower().rstrip(".")
    segments = [segment for segment in path.strip("/").removesuffix(".git").split("/") if segment]
    if len(segments) < 2:
        return None
    netloc = f"{host}:{port}" if port else host
    return RepositoryRef(
        repo_url=value,
        host=host,
        base_url=f"{scheme}://{netloc}",
        repository="/".join(segments),
    )


def infer_well_known(host: str) -> ProviderInference | None:
    provider_type = _WELL_KNOWN_HOSTS.get(host)
    return ProviderInference(provider_type, None, "host") if provider_type else None


async def probe_provider_host(base_url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> ProviderInference | None:
    """Identify a self-hosted GitLab (``/api/v4/version``) or Gitea (``/api/v1/version``)."""
    import httpx

    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            response = await client.get(f"{base}/api/v4/version")
            # GitLab answers 401 without a token, and {"version", "revision"} with one.
            if response.status_code == 401 or (response.status_code == 200 and "revision" in response.json()):
                return ProviderInference("gitlab", f"{base}/api/v4", "probe")
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        try:
            response = await client.get(f"{base}/api/v1/version")
            if response.status_code == 200 and "version" in response.json():
                return ProviderInference("gitea", f"{base}/api/v1", "probe")
        except (httpx.HTTPError, ValueError, TypeError):
            pass
    return None


def inferred_provider_id(host: str) -> str:
    slug = _PROVIDER_ID_UNSAFE.sub("-", host).strip("-.") or "host"
    return f"{INFERRED_PROVIDER_PREFIX}{slug}"[:128]


def provider_host(config: GitOpsProviderConfig) -> str | None:
    """Repository host served by a provider configuration (as discovery matches it)."""
    provider_type = config.provider_type.strip().lower()
    if not config.api_base_url:
        return {"github": "github.com", "gitlab": "gitlab.com"}.get(provider_type)
    try:
        host = urlsplit(config.api_base_url).hostname
    except ValueError:
        return None
    if host is None:
        return None
    host = host.lower().rstrip(".")
    return "github.com" if provider_type == "github" and host == "api.github.com" else host


def normalize_credentials_host(value: str | None) -> str | None:
    """Host named by the default Secret's ``host``/``apiBaseUrl`` key, as repositories are keyed."""
    text = (value or "").strip()
    if not text or any(character.isspace() for character in text):
        return None
    try:
        host = urlsplit(text if "://" in text else f"https://{text}").hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return "github.com" if host == "api.github.com" else host


def allowlist_matches(repository: str, patterns: Iterable[str]) -> bool:
    from greenkube.automation.git.base import repository_matches_allowlist

    return any(
        pattern != DISCOVERED_REPOSITORIES and repository_matches_allowlist(repository, pattern) for pattern in patterns
    )


# --------------------------------------------------------------------------- credentials


class CredentialsInspector:
    """Checks which mounted Secret keys exist, without reading their values into memory."""

    def __init__(self, mount_path: str | Path | None = None) -> None:
        from greenkube.automation.git.k8s_secret_resolver import MountedSecretFileResolver

        self._resolver = MountedSecretFileResolver(mount_path)

    def _present(self, namespace: str | None, name: str, key: str) -> bool:
        try:
            path = self._resolver.path_for_reference(namespace, name, key)
            return path.is_file() and path.stat().st_size > 0
        except Exception:
            return False

    def default_credentials(self, secret_name: str) -> str | None:
        """``token``, ``github_app`` (appId + privateKey) or None when the Secret is absent."""
        if self._present(None, secret_name, DEFAULT_CREDENTIALS_KEY):
            return "token"
        if self._present(None, secret_name, "appId") and self._present(None, secret_name, "privateKey"):
            return "github_app"
        return None

    def default_credentials_host(self, secret_name: str) -> str | None:
        """Host the default token is bound to (``host`` key, else ``apiBaseUrl``); not a secret."""
        for key in DEFAULT_CREDENTIALS_HOST_KEYS:
            try:
                path = self._resolver.path_for_reference(None, secret_name, key)
                if not path.is_file():
                    continue
                with path.open("rb") as handle:
                    raw = handle.read(_MAX_HOST_VALUE_BYTES)
            except Exception:
                continue
            host = normalize_credentials_host(raw.decode("utf-8", errors="replace"))
            if host:
                return host
        return None

    def provider_has_credentials(self, config: GitOpsProviderConfig) -> bool:
        if not config.secret_name or not config.secret_key:
            return False
        return self._present(config.secret_namespace, config.secret_name, config.secret_key)


# --------------------------------------------------------------------------- snapshot


@dataclass(frozen=True)
class CapabilityState:
    state: str
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.state == STATE_ACTIVE


@dataclass(frozen=True)
class RepositoryState:
    """Capability state of one discovered (or administrator-allowed) repository."""

    repo_url: str
    host: str
    repository: str
    provider_id: str | None
    provider_type: str | None
    api_base_url: str | None
    inferred_from: str
    allowlisted: bool
    credentials: bool
    discovered: bool
    preview: CapabilityState
    delivery: CapabilityState

    def payload(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "repository": self.repository,
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "api_base_url": self.api_base_url,
            "inferred_from": self.inferred_from,
            "allowlisted": self.allowlisted,
            "credentials": self.credentials,
            "discovered": self.discovered,
            "preview": {"state": self.preview.state, "reason": self.preview.reason},
            "delivery": {"state": self.delivery.state, "reason": self.delivery.reason},
        }


@dataclass(frozen=True)
class ReadinessSnapshot:
    refreshed_at: datetime
    capabilities: Mapping[str, CapabilityState]
    repositories: tuple[RepositoryState, ...] = ()

    def rows(self) -> list[CapabilityRow]:
        rows = [
            CapabilityRow(
                SCOPE_GLOBAL,
                name,
                state.state,
                {"reason": state.reason, **state.details},
            )
            for name, state in self.capabilities.items()
        ]
        rows.extend(
            CapabilityRow(SCOPE_REPOSITORY, repo.repo_url, repo.delivery.state, repo.payload())
            for repo in self.repositories
        )
        return rows


class _ReadinessRepository(Protocol):
    async def list_workload_bindings(self, *, cluster: str | None = None) -> list[WorkloadBinding]: ...

    async def list_provider_configs(self, *, enabled_only: bool = False) -> list[GitOpsProviderConfig]: ...

    async def save_provider_config(self, provider: GitOpsProviderConfig) -> GitOpsProviderConfig: ...

    async def list_provider_allowlists(self, provider_id: str) -> list[str]: ...

    async def add_provider_allowlist(self, provider_id: str, repository_pattern: str) -> None: ...

    async def remove_provider_allowlist(self, provider_id: str, repository_pattern: str) -> bool: ...


class _CapabilityStore(Protocol):
    async def replace_snapshot(self, rows: Sequence[CapabilityRow], *, now: datetime | None = None) -> None: ...

    async def list_allowed_repositories(self) -> list[AllowedRepository]: ...


@dataclass
class _HostPlan:
    host: str
    base_url: str
    refs: dict[str, RepositoryRef] = field(default_factory=dict)
    explicit: list[GitOpsProviderConfig] = field(default_factory=list)
    inference: ProviderInference | None = None
    #: The default credentials may be sent to this host (see ``_bind_default_credentials``).
    credentials_bound: bool = False
    credentials_reason: str | None = None


class AutomationReadiness:
    """Computes, applies and persists the automation capabilities (F12)."""

    def __init__(
        self,
        *,
        gitops_repository: _ReadinessRepository,
        capability_store: _CapabilityStore,
        cluster: str,
        config: Any,
        credentials: CredentialsInspector | None = None,
        prober: HostProber | None = None,
        discovery_status: Callable[[], Mapping[str, Any] | None] | None = None,
        request_discovery: Callable[[], Awaitable[Any]] | None = None,
        renderer_status: Callable[[], Awaitable[Mapping[str, Any] | None]] | None = None,
        webhooks_configured: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = gitops_repository
        self._store = capability_store
        self._cluster = cluster
        self._config = config
        self._credentials = credentials or CredentialsInspector()
        self._prober = prober or probe_provider_host
        self._discovery_status = discovery_status
        self._request_discovery = request_discovery
        self._renderer_status = renderer_status
        self._webhooks_configured = webhooks_configured or (
            lambda: bool(os.getenv("GITOPS_WEBHOOK_SECRET_FILE") or os.getenv("GITOPS_WEBHOOK_SECRET_DIR"))
        )
        self._clock = clock
        self._monotonic = monotonic
        self._probes: dict[str, tuple[ProviderInference | None, float]] = {}
        self._snapshot: ReadinessSnapshot | None = None

    # -- state read by the controller loops ------------------------------------------

    @property
    def snapshot(self) -> ReadinessSnapshot | None:
        return self._snapshot

    @property
    def delivery_active(self) -> bool:
        """At least one repository is deliverable and delivery is not paused."""
        snapshot = self._snapshot
        return snapshot is not None and snapshot.capabilities["delivery"].active

    @property
    def tracking_active(self) -> bool:
        """Open pull requests can be tracked (a provider has credentials, or webhooks)."""
        snapshot = self._snapshot
        return snapshot is not None and snapshot.capabilities["sync_tracking"].active

    # -- reconciliation ---------------------------------------------------------------

    async def run_once(self) -> ReadinessSnapshot:
        default_secret = str(getattr(self._config, "GITOPS_DEFAULT_CREDENTIALS_SECRET", "") or "")
        default_kind = self._credentials.default_credentials(default_secret) if default_secret else None
        patterns = tuple(getattr(self._config, "automation_repository_allowlist", (DISCOVERED_REPOSITORIES,)))
        allowed = await self._store.list_allowed_repositories()

        bindings = await self._repository.list_workload_bindings(cluster=self._cluster)
        providers = await self._repository.list_provider_configs()
        hosts = await self._plan_hosts(bindings, providers, allowed)
        credentials_host = (
            self._credentials.default_credentials_host(default_secret) if default_secret and default_kind else None
        )
        _bind_default_credentials(hosts, credentials_host)

        changed = await self._apply_inferred_providers(
            hosts, providers, default_secret, default_kind, patterns, allowed
        )
        changed = await self._expand_explicit_allowlists(hosts, allowed) or changed
        if changed and self._request_discovery is not None:
            try:
                await self._request_discovery()
            except Exception as error:  # discovery logs its own failures
                logger.info("Readiness-triggered discovery refresh failed (%s).", type(error).__name__)
            bindings = await self._repository.list_workload_bindings(cluster=self._cluster)
            providers = await self._repository.list_provider_configs()

        snapshot = await self._compute(hosts, bindings, providers, patterns, default_kind)
        await self._store.replace_snapshot(snapshot.rows(), now=snapshot.refreshed_at)
        for name in CAPABILITIES:
            CAPABILITY_ACTIVE.labels(capability=name).set(1 if snapshot.capabilities[name].active else 0)
        previous = self._snapshot
        self._snapshot = snapshot
        if previous is None or previous.capabilities["delivery"].state != snapshot.capabilities["delivery"].state:
            delivery = snapshot.capabilities["delivery"]
            logger.info("Pull-request delivery is %s (%s).", delivery.state, delivery.reason or "ready")
        return snapshot

    async def _plan_hosts(
        self,
        bindings: Sequence[WorkloadBinding],
        providers: Sequence[GitOpsProviderConfig],
        allowed: Sequence[AllowedRepository],
    ) -> dict[str, _HostPlan]:
        hosts: dict[str, _HostPlan] = {}

        def add(ref: RepositoryRef) -> None:
            plan = hosts.setdefault(ref.host, _HostPlan(ref.host, ref.base_url))
            plan.refs.setdefault(ref.repo_url, ref)

        for url in _repository_urls(bindings):
            ref = repository_ref(url)
            if ref is not None:
                add(ref)
        for entry in allowed:
            ref = repository_ref(entry.repo_url)
            if ref is not None:
                add(ref)
        for config in providers:
            if config.id == UNRESOLVED_PROVIDER_ID or config.id.startswith(INFERRED_PROVIDER_PREFIX):
                continue
            host = provider_host(config)
            if host in hosts:
                hosts[host].explicit.append(config)
        for plan in hosts.values():
            if plan.explicit:
                continue
            plan.inference = infer_well_known(plan.host) or await self._probe(plan.base_url)
        return hosts

    async def _probe(self, base_url: str) -> ProviderInference | None:
        cached = self._probes.get(base_url)
        now = self._monotonic()
        if cached is not None and (cached[0] is not None or now - cached[1] < PROBE_RETRY_SECONDS):
            return cached[0]
        try:
            result = await asyncio.wait_for(self._prober(base_url), timeout=PROBE_TIMEOUT_SECONDS * 3)
        except Exception as error:
            logger.info("Provider probe of %s failed (%s).", base_url, type(error).__name__)
            result = None
        self._probes[base_url] = (result, now)
        return result

    async def _apply_inferred_providers(
        self,
        hosts: Mapping[str, _HostPlan],
        providers: Sequence[GitOpsProviderConfig],
        default_secret: str,
        default_kind: str | None,
        patterns: Sequence[str],
        allowed: Sequence[AllowedRepository],
    ) -> bool:
        changed = False
        existing = {p.id: p for p in providers if p.id.startswith(INFERRED_PROVIDER_PREFIX)}
        wanted: set[str] = set()
        allowed_by_url = {entry.repo_url for entry in allowed}
        for plan in hosts.values():
            if plan.explicit or plan.inference is None or not default_secret:
                continue
            provider_id = inferred_provider_id(plan.host)
            wanted.add(provider_id)
            # An unbound host gets no Secret reference at all: its provider stays
            # disabled and nothing can send the default token there.
            bound = plan.credentials_bound
            desired = GitOpsProviderConfig(
                id=provider_id,
                provider_type=plan.inference.provider_type,
                api_base_url=plan.inference.api_base_url,
                secret_name=default_secret if bound else None,
                secret_key=DEFAULT_CREDENTIALS_KEY if bound else None,
                enabled=bound and default_kind is not None,
            )
            previous = existing.get(provider_id)
            if previous is None or _connection(previous) != _connection(desired):
                if previous is not None and previous.enabled == desired.enabled:
                    desired = replace(
                        desired,
                        status=previous.status,
                        last_checked_at=previous.last_checked_at,
                        last_error_code=previous.last_error_code,
                    )
                await self._repository.save_provider_config(desired)
                changed = True
            names = {
                ref.repository
                for ref in plan.refs.values()
                if DISCOVERED_REPOSITORIES in patterns
                or allowlist_matches(ref.repository, patterns)
                or ref.repo_url in allowed_by_url
            }
            names.update(pattern for pattern in patterns if pattern != DISCOVERED_REPOSITORIES)
            changed = await self._reconcile_allowlist(provider_id, names, exact=True) or changed
        for provider_id, previous in existing.items():
            if provider_id in wanted:
                continue
            if previous.enabled:
                await self._repository.save_provider_config(replace(previous, enabled=False))
                changed = True
            changed = await self._reconcile_allowlist(provider_id, set(), exact=True) or changed
        return changed

    async def _expand_explicit_allowlists(
        self, hosts: Mapping[str, _HostPlan], allowed: Sequence[AllowedRepository]
    ) -> bool:
        """Add discovered (``@discovered``) and administrator-allowed repositories to explicit providers.

        Entries are only added, never removed. ``reconcile_gitops_providers``
        drops the patterns that are not in the Helm values when the controller
        restarts; the next readiness tick (within 60 s) adds these again, so the
        registry and the snapshot agree after one tick.
        """
        changed = False
        allowed_urls = {entry.repo_url for entry in allowed}
        for plan in hosts.values():
            admin_names = {ref.repository for ref in plan.refs.values() if ref.repo_url in allowed_urls}
            for config in plan.explicit:
                current = set(await self._repository.list_provider_allowlists(config.id))
                names = set(admin_names)
                if DISCOVERED_REPOSITORIES in current:
                    names.update(ref.repository for ref in plan.refs.values())
                if names - current:
                    changed = await self._reconcile_allowlist(config.id, names | current, exact=False) or changed
        return changed

    async def _reconcile_allowlist(self, provider_id: str, desired: set[str], *, exact: bool) -> bool:
        current = set(await self._repository.list_provider_allowlists(provider_id))
        changed = False
        for pattern in sorted(desired - current):
            await self._repository.add_provider_allowlist(provider_id, pattern)
            changed = True
        if exact:
            for pattern in sorted(current - desired):
                await self._repository.remove_provider_allowlist(provider_id, pattern)
                changed = True
        return changed

    async def _compute(
        self,
        hosts: Mapping[str, _HostPlan],
        bindings: Sequence[WorkloadBinding],
        providers: Sequence[GitOpsProviderConfig],
        patterns: Sequence[str],
        default_kind: str | None,
    ) -> ReadinessSnapshot:
        now = self._clock()
        paused = bool(getattr(self._config, "automation_delivery_paused", False))
        by_id = {p.id: p for p in providers}
        resolved_urls = _resolved_repository_urls(bindings)
        discovered_urls = set(_repository_urls(bindings))
        allowlists: dict[str, set[str]] = {}

        async def allowlist_of(provider_id: str) -> set[str]:
            if provider_id not in allowlists:
                allowlists[provider_id] = set(await self._repository.list_provider_allowlists(provider_id))
            return allowlists[provider_id]

        repositories: list[RepositoryState] = []
        for plan in sorted(hosts.values(), key=lambda item: item.host):
            for ref in sorted(plan.refs.values(), key=lambda item: item.repo_url):
                provider: GitOpsProviderConfig | None
                if plan.explicit:
                    provider = next((p for p in plan.explicit if p.enabled), plan.explicit[0])
                    inferred_from = "configured"
                else:
                    provider = by_id.get(inferred_provider_id(plan.host)) if plan.inference else None
                    inferred_from = plan.inference.inferred_from if plan.inference else "unknown"
                if provider is not None:
                    # The allowlist the provider registry actually enforces.
                    allowlisted = allowlist_matches(ref.repository, await allowlist_of(provider.id))
                else:
                    allowlisted = (DISCOVERED_REPOSITORIES in patterns and ref.repo_url in discovered_urls) or (
                        allowlist_matches(ref.repository, patterns)
                    )
                credentials = bool(
                    provider and provider.enabled and self._credentials.provider_has_credentials(provider)
                )
                preview = _preview_state(
                    provider=provider,
                    known=bool(plan.explicit or plan.inference),
                    allowlisted=allowlisted,
                    credentials=credentials,
                    default_kind=default_kind,
                    resolved=ref.repo_url in resolved_urls,
                    credentials_reason=None if plan.explicit else plan.credentials_reason,
                )
                if paused:
                    delivery = CapabilityState(STATE_DORMANT, "delivery_paused")
                elif preview.active:
                    delivery = CapabilityState(STATE_ACTIVE)
                else:
                    delivery = CapabilityState(STATE_DORMANT, preview.reason)
                repositories.append(
                    RepositoryState(
                        repo_url=ref.repo_url,
                        host=ref.host,
                        repository=ref.repository,
                        provider_id=provider.id if provider else None,
                        provider_type=(provider.provider_type if provider else None)
                        or (plan.inference.provider_type if plan.inference else None),
                        api_base_url=(provider.api_base_url if provider else None)
                        or (plan.inference.api_base_url if plan.inference else None),
                        inferred_from=inferred_from,
                        allowlisted=allowlisted,
                        credentials=credentials,
                        discovered=ref.repo_url in discovered_urls,
                        preview=preview,
                        delivery=delivery,
                    )
                )

        capabilities = {
            "discovery": self._discovery_state(bindings),
            "preview": _aggregate(repositories, "preview", empty_reason="no_repository_discovered"),
            "delivery": (
                CapabilityState(STATE_DORMANT, "delivery_paused")
                if paused
                else _aggregate(repositories, "delivery", empty_reason="no_repository_discovered")
            ),
            "sync_tracking": self._tracking_state(providers, default_kind, hosts),
            "webhooks": (
                CapabilityState(STATE_ACTIVE)
                if self._webhooks_configured()
                else CapabilityState(STATE_DORMANT, "webhook_secret_missing")
            ),
            "measurement": CapabilityState(STATE_ACTIVE),
            "renderer": await self._renderer_state(),
        }
        return ReadinessSnapshot(refreshed_at=now, capabilities=capabilities, repositories=tuple(repositories))

    def _discovery_state(self, bindings: Sequence[WorkloadBinding]) -> CapabilityState:
        status = self._discovery_status() if self._discovery_status is not None else None
        controllers = []
        if status:
            controllers = [name for name in _CONTROLLER_CAPABILITIES if _available(status.get(name))]
        if controllers or bindings:
            return CapabilityState(STATE_ACTIVE, details={"controllers": controllers})
        if status is None:
            return CapabilityState(STATE_PENDING, "discovery_not_run")
        return CapabilityState(STATE_DORMANT, "no_gitops_controller")

    def _tracking_state(
        self, providers: Sequence[GitOpsProviderConfig], default_kind: str | None, hosts: Mapping[str, _HostPlan]
    ) -> CapabilityState:
        credentialed = [
            p.id
            for p in providers
            if p.id != UNRESOLVED_PROVIDER_ID and p.enabled and self._credentials.provider_has_credentials(p)
        ]
        if credentialed or self._webhooks_configured():
            return CapabilityState(STATE_ACTIVE, details={"providers": credentialed})
        if default_kind is not None:
            # Credentials are mounted, but no discovered repository uses them yet.
            unbound = sorted({plan.credentials_reason for plan in hosts.values() if plan.credentials_reason})
            return CapabilityState(STATE_DORMANT, unbound[0] if unbound else "no_repository_discovered")
        return CapabilityState(STATE_DORMANT, "credentials_missing")

    async def _renderer_state(self) -> CapabilityState:
        if self._renderer_status is None:
            return CapabilityState(STATE_DORMANT, "renderer_not_configured")
        try:
            status = await self._renderer_status()
        except Exception:
            status = None
        if status is None:
            return CapabilityState(STATE_DORMANT, "renderer_unreachable")
        egress = status.get("egress") if isinstance(status.get("egress"), Mapping) else {}
        enforced = egress.get("enforced") if isinstance(egress, Mapping) else None
        details = {"egress_enforced": enforced, "egress_checked_at": egress.get("checked_at") if egress else None}
        if enforced is False:
            return CapabilityState(STATE_ACTIVE, "renderer_egress_unenforced", details)
        return CapabilityState(STATE_ACTIVE, None, details)


def _bind_default_credentials(hosts: Mapping[str, _HostPlan], credentials_host: str | None) -> None:
    """Decide which inferred host may receive the default token (at most one).

    The token belongs to one Git host. Sending it to every host a GitOps source
    references would hand it to whoever can create an Argo CD Application or a
    Flux GitRepository pointing at a server they control. So:

    * the Secret names its host (``host`` or ``apiBaseUrl`` key): only that host;
    * otherwise: only when github.com or gitlab.com is the single inferred host.
      A probe-inferred self-hosted host always needs the ``host`` key.
    """
    candidates = [plan for plan in hosts.values() if not plan.explicit and plan.inference is not None]
    for plan in candidates:
        if credentials_host is not None:
            plan.credentials_bound = plan.host == credentials_host
            plan.credentials_reason = None if plan.credentials_bound else CREDENTIALS_HOST_MISMATCH
        elif len(candidates) == 1 and plan.host in _WELL_KNOWN_HOSTS:
            plan.credentials_bound, plan.credentials_reason = True, None
        else:
            plan.credentials_bound, plan.credentials_reason = False, CREDENTIALS_HOST_AMBIGUOUS


def _connection(config: GitOpsProviderConfig) -> tuple[Any, ...]:
    return (
        config.provider_type,
        config.api_base_url,
        config.secret_namespace,
        config.secret_name,
        config.secret_key,
        config.enabled,
    )


def _available(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return bool(value.get("available"))
    return bool(getattr(value, "available", False))


def _repository_urls(bindings: Sequence[WorkloadBinding]) -> list[str]:
    urls: list[str] = []
    for binding in bindings:
        for source in (binding.source, *binding.candidates):
            url = getattr(source, "repo_url", None) if source is not None else None
            if url and url not in urls:
                urls.append(url)
    return urls


def _resolved_repository_urls(bindings: Sequence[WorkloadBinding]) -> set[str]:
    return {
        binding.source.repo_url
        for binding in bindings
        if binding.status == BindingStatus.RESOLVED
        and binding.source is not None
        and binding.source.provider_id != UNRESOLVED_PROVIDER_ID
    }


def _preview_state(
    *,
    provider: GitOpsProviderConfig | None,
    known: bool,
    allowlisted: bool,
    credentials: bool,
    default_kind: str | None,
    resolved: bool,
    credentials_reason: str | None = None,
) -> CapabilityState:
    if not known:
        return CapabilityState(STATE_DORMANT, "provider_unknown")
    if not allowlisted:
        return CapabilityState(STATE_DORMANT, "not_allowlisted")
    if provider is None or not credentials:
        if default_kind is not None and credentials_reason:
            return CapabilityState(STATE_DORMANT, credentials_reason)
        if provider is not None and provider.id.startswith(INFERRED_PROVIDER_PREFIX) and default_kind == "github_app":
            return CapabilityState(STATE_DORMANT, "github_app_not_supported")
        return CapabilityState(STATE_DORMANT, "credentials_missing")
    if not resolved:
        return CapabilityState(STATE_PENDING, "binding_unresolved")
    return CapabilityState(STATE_ACTIVE)


def _aggregate(repositories: Sequence[RepositoryState], attribute: str, *, empty_reason: str) -> CapabilityState:
    if not repositories:
        return CapabilityState(STATE_DORMANT, empty_reason)
    states = [getattr(repo, attribute) for repo in repositories]
    active = sum(1 for state in states if state.active)
    details = {"active_repositories": active, "repositories": len(states)}
    if active:
        return CapabilityState(STATE_ACTIVE, details=details)
    reasons = [state.reason for state in states if state.reason]
    # The most common blocker explains the state best.
    reason = max(set(reasons), key=reasons.count) if reasons else None
    return CapabilityState(STATE_DORMANT, reason, details)


def renderer_status_reader(
    renderer_url: str, *, timeout: float = 3.0
) -> Callable[[], Awaitable[Mapping[str, Any] | None]]:
    """GET ``<renderer>/status`` (egress canary, F6); None when unreachable."""

    async def read() -> Mapping[str, Any] | None:
        import httpx

        if not renderer_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{renderer_url.rstrip('/')}/status")
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, Mapping) else None
        except (httpx.HTTPError, ValueError):
            return None

    return read


def renderer_egress_unenforced(row: CapabilityRow | None) -> bool:
    """Whether a persisted ``renderer`` capability row reports the egress canary unenforced (F6)."""
    if row is None:
        return False
    payload = row.payload or {}
    return payload.get("reason") == "renderer_egress_unenforced" or payload.get("egress_enforced") is False


def renderer_egress_probe(db_manager: Any) -> Callable[[], Awaitable[bool]]:
    """Reads the reconciler's last renderer state: True when previews must carry the F6 warning.

    It reads the stored snapshot only (never the renderer itself), so a plan
    preview costs one small query. The last known state is used even when the
    snapshot is old: an unenforced NetworkPolicy rarely fixes itself.
    """
    from greenkube.storage.automation_capability_repository import AutomationCapabilityRepository

    async def probe() -> bool:
        rows = await AutomationCapabilityRepository(db_manager).list_capabilities(SCOPE_GLOBAL)
        return renderer_egress_unenforced(next((row for row in rows if row.subject == "renderer"), None))

    return probe
