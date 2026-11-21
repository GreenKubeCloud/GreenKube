"""AutomationReadiness reconciler: always-on automation with capability activation (F12, F2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from greenkube.automation.readiness import (
    AutomationReadiness,
    CredentialsInspector,
    ProviderInference,
    _repository_urls,
    inferred_provider_id,
    normalize_credentials_host,
    repository_ref,
)
from greenkube.gitops.contracts import BindingStatus, ControllerKind, RendererKind, WorkloadSource
from greenkube.storage.automation_capability_repository import AllowedRepository
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, WorkloadBinding

GITHUB_REPO = "https://github.com/acme/gitops.git"
GITEA_REPO = "http://gitea.internal:3000/platform/apps.git"


def _source(
    repo_url: str, provider_id: str = "unresolved", status=BindingStatus.BLOCKED, name: str = "api"
) -> WorkloadSource:
    return WorkloadSource(
        id=f"source-{provider_id}-{repo_url}",
        cluster="default",
        namespace="payments",
        workload_kind="Deployment",
        workload_name=name,
        provider_id=provider_id,
        repo_url=repo_url,
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=1 if status == BindingStatus.RESOLVED else 0.1,
        status=status,
    )


def _binding(repo_url: str, *, provider_id: str | None = None, name: str = "api") -> WorkloadBinding:
    if provider_id is None:
        return WorkloadBinding(
            cluster="default",
            namespace="payments",
            workload_kind="Deployment",
            workload_name=name,
            status=BindingStatus.UNRESOLVED,
            confidence=0.1,
            candidates=(_source(repo_url, name=name),),
        )
    source = _source(repo_url, provider_id, BindingStatus.RESOLVED, name)
    return WorkloadBinding(
        cluster="default",
        namespace="payments",
        workload_kind="Deployment",
        workload_name=name,
        status=BindingStatus.RESOLVED,
        confidence=1,
        source=source,
    )


class FakeGitOpsRepository:
    """Provider rows, allowlists and bindings; discovery re-resolves against enabled providers."""

    def __init__(self, repo_urls: list[str], providers: list[GitOpsProviderConfig] | None = None) -> None:
        self.repo_urls = repo_urls
        self.providers = {p.id: p for p in providers or []}
        self.allowlists: dict[str, set[str]] = {}
        self.saved: list[GitOpsProviderConfig] = []

    async def list_workload_bindings(self, *, cluster=None):
        bindings = []
        for index, url in enumerate(self.repo_urls):
            provider = self._match(url)
            bindings.append(_binding(url, provider_id=provider, name=f"app-{index}"))
        return bindings

    def _match(self, url: str) -> str | None:
        ref = repository_ref(url)
        assert ref is not None
        for provider in self.providers.values():
            if not provider.enabled or provider.id == "unresolved":
                continue
            if provider.id == inferred_provider_id(ref.host) and ref.repository in self.allowlists.get(provider.id, ()):
                return provider.id
        return None

    async def list_provider_configs(self, *, enabled_only=False):
        return [p for p in self.providers.values() if p.enabled or not enabled_only]

    async def save_provider_config(self, provider):
        self.providers[provider.id] = provider
        self.saved.append(provider)
        return provider

    async def list_provider_allowlists(self, provider_id):
        return sorted(self.allowlists.get(provider_id, set()))

    async def add_provider_allowlist(self, provider_id, pattern):
        self.allowlists.setdefault(provider_id, set()).add(pattern)

    async def remove_provider_allowlist(self, provider_id, pattern):
        self.allowlists.get(provider_id, set()).discard(pattern)
        return True


class FakeStore:
    def __init__(self, allowed: list[AllowedRepository] | None = None) -> None:
        self.rows = []
        self.allowed = allowed or []

    async def replace_snapshot(self, rows, *, now=None):
        self.rows = list(rows)

    async def list_allowed_repositories(self):
        return list(self.allowed)


def _config(**overrides):
    values = {
        "GITOPS_DEFAULT_CREDENTIALS_SECRET": "greenkube-git-credentials",
        "automation_repository_allowlist": ("@discovered",),
        "automation_delivery_paused": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _readiness(repository, tmp_path, *, store=None, config=None, prober=None, discovery=None, triggered=None):
    async def request_discovery():
        if triggered is not None:
            triggered.append(True)

    async def renderer_status():
        return {"egress": {"enforced": True}}

    async def no_probe(base_url):
        return None

    return AutomationReadiness(
        gitops_repository=repository,
        capability_store=store or FakeStore(),
        cluster="default",
        config=config or _config(),
        credentials=CredentialsInspector(tmp_path),
        prober=prober or no_probe,
        discovery_status=lambda: discovery,
        request_discovery=request_discovery,
        renderer_status=renderer_status,
        webhooks_configured=lambda: False,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _write_token(tmp_path, secret="greenkube-git-credentials", key="token", value="t0k3n"):
    path = tmp_path / secret / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def test_repository_ref_keeps_scheme_port_and_nested_groups():
    ref = repository_ref("http://gitea.internal:3000/group/sub/apps.git")
    assert ref is not None
    assert (ref.host, ref.base_url, ref.repository) == (
        "gitea.internal",
        "http://gitea.internal:3000",
        "group/sub/apps",
    )
    ssh = repository_ref("git@github.com:acme/gitops.git")
    assert ssh is not None
    assert (ssh.host, ssh.base_url, ssh.repository) == ("github.com", "https://github.com", "acme/gitops")
    assert repository_ref("not a url") is None


async def test_discovered_github_repository_is_dormant_until_the_secret_exists(tmp_path):
    repository = FakeGitOpsRepository([GITHUB_REPO])
    triggered: list[bool] = []
    readiness = _readiness(repository, tmp_path, triggered=triggered, discovery={"argocd": {"available": True}})

    snapshot = await readiness.run_once()

    provider = repository.providers["inferred-github.com"]
    assert (provider.provider_type, provider.api_base_url, provider.enabled) == ("github", None, False)
    assert (provider.secret_name, provider.secret_key) == ("greenkube-git-credentials", "token")
    assert repository.allowlists["inferred-github.com"] == {"acme/gitops"}
    assert snapshot.capabilities["discovery"].active
    assert snapshot.capabilities["delivery"].state == "dormant"
    assert snapshot.capabilities["delivery"].reason == "credentials_missing"
    assert readiness.delivery_active is False
    (repo,) = snapshot.repositories
    assert (repo.inferred_from, repo.allowlisted, repo.credentials) == ("host", True, False)

    # Creating the Secret activates delivery on the next tick, without a restart.
    _write_token(tmp_path)
    triggered.clear()
    snapshot = await readiness.run_once()

    assert repository.providers["inferred-github.com"].enabled is True
    assert triggered == [True]
    assert snapshot.capabilities["delivery"].active
    assert snapshot.capabilities["sync_tracking"].active
    assert readiness.delivery_active is True and readiness.tracking_active is True

    # Deleting it makes delivery dormant again.
    (tmp_path / "greenkube-git-credentials" / "token").unlink()
    snapshot = await readiness.run_once()
    assert repository.providers["inferred-github.com"].enabled is False
    assert readiness.delivery_active is False
    assert snapshot.capabilities["delivery"].reason == "credentials_missing"


async def test_pause_delivery_is_the_only_off_switch(tmp_path):
    _write_token(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path, config=_config(automation_delivery_paused=True))

    snapshot = await readiness.run_once()

    assert snapshot.capabilities["discovery"].active
    assert snapshot.capabilities["preview"].active
    assert snapshot.capabilities["sync_tracking"].active
    assert snapshot.capabilities["measurement"].active
    assert snapshot.capabilities["delivery"].reason == "delivery_paused"
    assert readiness.delivery_active is False


async def test_self_hosted_hosts_are_probed_once(tmp_path):
    _write_token(tmp_path)
    _write_token(tmp_path, key="host", value="gitea.internal")
    probes: list[str] = []

    async def prober(base_url):
        probes.append(base_url)
        return ProviderInference("gitea", f"{base_url}/api/v1", "probe")

    repository = FakeGitOpsRepository([GITEA_REPO])
    readiness = _readiness(repository, tmp_path, prober=prober)

    await readiness.run_once()
    snapshot = await readiness.run_once()

    provider = repository.providers["inferred-gitea.internal"]
    assert (provider.provider_type, provider.api_base_url) == ("gitea", "http://gitea.internal:3000/api/v1")
    assert probes == ["http://gitea.internal:3000"]
    assert snapshot.repositories[0].inferred_from == "probe"
    assert snapshot.capabilities["delivery"].active


async def test_unknown_self_hosted_host_explains_itself(tmp_path):
    _write_token(tmp_path)
    repository = FakeGitOpsRepository(["https://git.example.test/a/b.git"])
    snapshot = await _readiness(repository, tmp_path).run_once()

    assert "inferred-git.example.test" not in repository.providers
    assert snapshot.repositories[0].preview.reason == "provider_unknown"
    assert snapshot.capabilities["delivery"].reason == "provider_unknown"


async def test_explicit_allowlist_narrows_discovered_repositories(tmp_path):
    _write_token(tmp_path)
    other = "https://github.com/acme/other.git"
    repository = FakeGitOpsRepository([GITHUB_REPO, other])
    readiness = _readiness(repository, tmp_path, config=_config(automation_repository_allowlist=("acme/gitops",)))

    snapshot = await readiness.run_once()

    assert repository.allowlists["inferred-github.com"] == {"acme/gitops"}
    states = {repo.repository: repo for repo in snapshot.repositories}
    assert states["acme/gitops"].delivery.active
    assert states["acme/other"].allowlisted is False
    assert states["acme/other"].delivery.reason == "not_allowlisted"


async def test_administrator_allowed_repositories_join_a_narrowed_allowlist(tmp_path):
    _write_token(tmp_path)
    other = "https://github.com/acme/other.git"
    allowed = [
        AllowedRepository(other, "github.com", "acme/other", "user:admin", datetime(2026, 1, 1, tzinfo=timezone.utc))
    ]
    repository = FakeGitOpsRepository([GITHUB_REPO, other])
    readiness = _readiness(
        repository,
        tmp_path,
        store=FakeStore(allowed),
        config=_config(automation_repository_allowlist=("acme/gitops",)),
    )

    snapshot = await readiness.run_once()

    assert repository.allowlists["inferred-github.com"] == {"acme/gitops", "acme/other"}
    assert all(repo.delivery.active for repo in snapshot.repositories)


async def test_explicit_providers_win_over_inference_and_expand_discovered(tmp_path):
    _write_token(tmp_path, secret="github-token")
    explicit = GitOpsProviderConfig(
        id="github-main",
        provider_type="github",
        api_base_url="https://api.github.com",
        secret_name="github-token",
        secret_key="token",
    )
    repository = FakeGitOpsRepository([GITHUB_REPO], [explicit])
    repository.allowlists["github-main"] = {"@discovered"}
    triggered: list[bool] = []

    snapshot = await _readiness(repository, tmp_path, triggered=triggered).run_once()

    assert not any(provider_id.startswith("inferred-") for provider_id in repository.providers)
    assert repository.allowlists["github-main"] == {"@discovered", "acme/gitops"}
    assert triggered == [True]
    (repo,) = snapshot.repositories
    assert (repo.provider_id, repo.inferred_from, repo.allowlisted, repo.credentials) == (
        "github-main",
        "configured",
        True,
        True,
    )


async def test_vanished_hosts_disable_their_inferred_provider(tmp_path):
    _write_token(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path)
    await readiness.run_once()
    assert repository.providers["inferred-github.com"].enabled

    repository.repo_urls = []
    snapshot = await readiness.run_once()
    assert snapshot.capabilities["sync_tracking"].reason == "no_repository_discovered"

    assert repository.providers["inferred-github.com"].enabled is False
    assert repository.allowlists["inferred-github.com"] == set()
    assert snapshot.capabilities["delivery"].reason == "no_repository_discovered"


async def test_discovery_states_and_renderer_warning(tmp_path):
    repository = FakeGitOpsRepository([])

    pending = await _readiness(repository, tmp_path, discovery=None).run_once()
    dormant = await _readiness(repository, tmp_path, discovery={"argocd": {"available": False}}).run_once()

    assert (pending.capabilities["discovery"].state, pending.capabilities["discovery"].reason) == (
        "pending",
        "discovery_not_run",
    )
    assert dormant.capabilities["discovery"].reason == "no_gitops_controller"

    readiness = _readiness(repository, tmp_path)

    async def unenforced():
        return {"egress": {"enforced": False, "checked_at": "2026-01-01T00:00:00Z"}}

    readiness._renderer_status = unenforced
    renderer = (await readiness.run_once()).capabilities["renderer"]
    assert renderer.active
    assert renderer.reason == "renderer_egress_unenforced"


async def test_snapshot_is_persisted_for_the_api(tmp_path):
    _write_token(tmp_path)
    store = FakeStore()
    await _readiness(FakeGitOpsRepository([GITHUB_REPO]), tmp_path, store=store).run_once()

    by_key = {(row.scope, row.subject): row for row in store.rows}
    assert by_key[("global", "delivery")].state == "active"
    repo_row = by_key[("repository", GITHUB_REPO)]
    assert repo_row.payload["provider_type"] == "github"
    assert repo_row.payload["allowlisted"] is True
    assert {name for scope, name in by_key if scope == "global"} == {
        "discovery",
        "preview",
        "delivery",
        "sync_tracking",
        "webhooks",
        "measurement",
        "renderer",
    }


def _write_github_app(tmp_path, *, app_id: bool = True) -> None:
    if app_id:
        _write_token(tmp_path, key="appId", value="123")
    _write_token(tmp_path, key="privateKey", value="-----BEGIN RSA PRIVATE KEY-----\n...")


async def test_github_app_credentials_bind_to_the_inferred_github_provider(tmp_path):
    """F3: appId + privateKey in the default Secret activate the inferred github.com provider."""
    _write_github_app(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path)

    await readiness.run_once()
    snapshot = await readiness.run_once()

    github = repository.providers["inferred-github.com"]
    assert _secret_ref(github) == ("greenkube-git-credentials", "privateKey", True)
    assert snapshot.repositories[0].credentials is True
    assert snapshot.repositories[0].preview.active
    assert snapshot.capabilities["delivery"].active


async def test_github_app_credentials_are_never_bound_to_another_provider_type(tmp_path):
    _write_github_app(tmp_path)
    _write_token(tmp_path, key="host", value="gitlab.com")
    repository = FakeGitOpsRepository([GITLAB_REPO])

    snapshot = await _readiness(repository, tmp_path).run_once()

    assert _secret_ref(repository.providers["inferred-gitlab.com"]) == (None, None, False)
    assert snapshot.repositories[0].preview.reason == "github_app_host_unsupported"


async def test_github_app_follows_the_token_host_binding_rules(tmp_path):
    _write_github_app(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO, ATTACKER_REPO])

    snapshot = await _readiness(repository, tmp_path, prober=_gitlab_prober).run_once()

    assert _secret_ref(repository.providers["inferred-github.com"]) == (None, None, False)
    states = {repo.host: repo for repo in snapshot.repositories}
    assert states["github.com"].preview.reason == "credentials_host_ambiguous"
    assert states["evil.example"].preview.reason == "github_app_host_unsupported"


async def test_a_github_app_private_key_without_its_app_id_is_not_credentials(tmp_path):
    _write_github_app(tmp_path, app_id=False)

    snapshot = await _readiness(FakeGitOpsRepository([GITHUB_REPO]), tmp_path).run_once()

    assert snapshot.repositories[0].preview.reason == "credentials_missing"
    explicit = GitOpsProviderConfig(
        id="github-app", provider_type="github", secret_name="greenkube-git-credentials", secret_key="privateKey"
    )
    assert CredentialsInspector(tmp_path).provider_has_credentials(explicit) is False
    _write_token(tmp_path, key="appId", value="123")
    assert CredentialsInspector(tmp_path).provider_has_credentials(explicit) is True


@pytest.mark.parametrize(("mode", "write_required"), [("direct", True), ("patch", False), ("ci", False)])
async def test_delivery_capability_reports_the_delivery_mode(tmp_path, mode, write_required):
    """F4: patch and ci modes need no write credentials; the delivery capability says so."""
    _write_token(tmp_path)
    readiness = _readiness(FakeGitOpsRepository([GITHUB_REPO]), tmp_path, config=_config(AUTOMATION_DELIVERY_MODE=mode))

    await readiness.run_once()
    snapshot = await readiness.run_once()

    delivery = snapshot.capabilities["delivery"]
    assert delivery.active
    assert delivery.details["mode"] == mode
    assert delivery.details["write_credentials_required"] is write_required
    global_row = next(row for row in snapshot.rows() if row.subject == "delivery")
    assert global_row.payload["mode"] == mode


@pytest.mark.parametrize("existing_status", ["verified", "error"])
async def test_reenabling_keeps_last_verified_status_only_when_unchanged(tmp_path, existing_status):
    _write_token(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path)
    await readiness.run_once()
    provider = repository.providers["inferred-github.com"]
    repository.providers[provider.id] = replace(provider, status=existing_status)

    await readiness.run_once()

    # Nothing changed: the provider row (and what a real call verified) is left alone.
    assert repository.providers[provider.id].status == existing_status


# --------------------------------------------------------------- default credentials host binding

GITLAB_REPO = "https://gitlab.com/acme/infra.git"
ATTACKER_REPO = "https://evil.example/x/y.git"


async def _gitlab_prober(base_url):
    return ProviderInference("gitlab", f"{base_url}/api/v4", "probe")


def _secret_ref(provider: GitOpsProviderConfig) -> tuple:
    return (provider.secret_name, provider.secret_key, provider.enabled)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("github.com", "github.com"),
        ("https://api.github.com", "github.com"),
        ("https://GitLab.Example.com:8443/api/v4\n", "gitlab.example.com"),
        ("", None),
        ("two words", None),
    ],
)
def test_credentials_host_is_normalized_like_repository_hosts(value, expected):
    assert normalize_credentials_host(value) == expected


async def test_token_is_never_sent_to_an_attacker_controlled_host(tmp_path):
    """A GitOps source pointing at a host someone else controls must not receive the token."""
    _write_token(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO, ATTACKER_REPO])

    snapshot = await _readiness(repository, tmp_path, prober=_gitlab_prober).run_once()

    attacker = repository.providers["inferred-evil.example"]
    github = repository.providers["inferred-github.com"]
    assert _secret_ref(attacker) == (None, None, False)
    assert _secret_ref(github) == (None, None, False)
    states = {repo.host: repo for repo in snapshot.repositories}
    assert states["evil.example"].preview.reason == "credentials_host_ambiguous"
    assert states["github.com"].preview.reason == "credentials_host_ambiguous"
    assert snapshot.capabilities["delivery"].reason == "credentials_host_ambiguous"
    assert snapshot.capabilities["sync_tracking"].reason == "credentials_host_ambiguous"


async def test_host_key_binds_the_token_to_one_host(tmp_path):
    _write_token(tmp_path)
    _write_token(tmp_path, key="host", value="github.com")
    repository = FakeGitOpsRepository([GITHUB_REPO, ATTACKER_REPO])

    snapshot = await _readiness(repository, tmp_path, prober=_gitlab_prober).run_once()

    assert _secret_ref(repository.providers["inferred-github.com"]) == ("greenkube-git-credentials", "token", True)
    assert _secret_ref(repository.providers["inferred-evil.example"]) == (None, None, False)
    states = {repo.host: repo for repo in snapshot.repositories}
    assert states["github.com"].delivery.active
    assert states["evil.example"].preview.reason == "credentials_host_mismatch"
    assert snapshot.capabilities["delivery"].active


async def test_github_token_is_not_sent_to_gitlab(tmp_path):
    _write_token(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO, GITLAB_REPO])
    readiness = _readiness(repository, tmp_path)

    await readiness.run_once()
    assert _secret_ref(repository.providers["inferred-github.com"]) == (None, None, False)
    assert _secret_ref(repository.providers["inferred-gitlab.com"]) == (None, None, False)

    _write_token(tmp_path, key="host", value="https://gitlab.com")
    snapshot = await readiness.run_once()
    assert _secret_ref(repository.providers["inferred-github.com"]) == (None, None, False)
    assert _secret_ref(repository.providers["inferred-gitlab.com"]) == ("greenkube-git-credentials", "token", True)
    states = {repo.host: repo for repo in snapshot.repositories}
    assert states["gitlab.com"].delivery.active
    assert states["github.com"].preview.reason == "credentials_host_mismatch"


async def test_a_previously_bound_provider_loses_the_token_when_a_second_host_appears(tmp_path):
    _write_token(tmp_path)
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path, prober=_gitlab_prober)
    await readiness.run_once()
    assert repository.providers["inferred-github.com"].enabled

    repository.repo_urls = [GITHUB_REPO, ATTACKER_REPO]
    await readiness.run_once()

    assert _secret_ref(repository.providers["inferred-github.com"]) == (None, None, False)
    assert readiness.delivery_active is False


async def test_self_hosted_host_needs_the_host_key(tmp_path):
    _write_token(tmp_path)
    repository = FakeGitOpsRepository([GITEA_REPO])

    async def prober(base_url):
        return ProviderInference("gitea", f"{base_url}/api/v1", "probe")

    snapshot = await _readiness(repository, tmp_path, prober=prober).run_once()

    assert _secret_ref(repository.providers["inferred-gitea.internal"]) == (None, None, False)
    assert snapshot.repositories[0].preview.reason == "credentials_host_ambiguous"


async def test_administrator_allowed_repository_reaches_an_explicit_provider_allowlist(tmp_path):
    _write_token(tmp_path, secret="github-token")
    other = "https://github.com/acme/other.git"
    explicit = GitOpsProviderConfig(
        id="gh",
        provider_type="github",
        api_base_url="https://api.github.com",
        secret_name="github-token",
        secret_key="token",
    )
    repository = FakeGitOpsRepository([GITHUB_REPO, other], [explicit])
    repository.allowlists["gh"] = {"acme/gitops"}
    allowed = [
        AllowedRepository(other, "github.com", "acme/other", "user:admin", datetime(2026, 1, 1, tzinfo=timezone.utc))
    ]
    triggered: list[bool] = []

    snapshot = await _readiness(repository, tmp_path, store=FakeStore(allowed), triggered=triggered).run_once()

    # The registry enforces the provider allowlist: the snapshot reports exactly that.
    assert repository.allowlists["gh"] == {"acme/gitops", "acme/other"}
    assert triggered == [True]
    assert all(repo.allowlisted for repo in snapshot.repositories)


async def test_explicit_provider_snapshot_follows_its_database_allowlist(tmp_path):
    _write_token(tmp_path, secret="github-token")
    other = "https://github.com/acme/other.git"
    explicit = GitOpsProviderConfig(
        id="gh",
        provider_type="github",
        api_base_url="https://api.github.com",
        secret_name="github-token",
        secret_key="token",
    )
    repository = FakeGitOpsRepository([GITHUB_REPO, other], [explicit])
    repository.allowlists["gh"] = {"acme/gitops"}

    snapshot = await _readiness(repository, tmp_path).run_once()

    states = {repo.repository: repo for repo in snapshot.repositories}
    assert states["acme/gitops"].allowlisted is True
    assert states["acme/other"].allowlisted is False
    assert states["acme/other"].preview.reason == "not_allowlisted"


async def test_discovery_reports_controller_families(tmp_path):
    discovery = {
        "argocd": {"available": True},
        "flux_kustomization": {"available": True},
        "flux_helm_release": {"available": True},
    }
    snapshot = await _readiness(FakeGitOpsRepository([]), tmp_path, discovery=discovery).run_once()

    details = snapshot.capabilities["discovery"].details
    assert details["controllers"] == ["argocd", "flux"]
    assert details["source_kinds"] == ["argocd", "flux_kustomization", "flux_helm_release"]


OTHER_GITHUB_REPO = "https://github.com/acme/legacy.git"


def _app_selection(*names: str, calls: list | None = None, fail: list | None = None):
    async def list_repositories(credentials, api_base_url):
        if calls is not None:
            calls.append((credentials.app_id, api_base_url))
        if fail:
            raise RuntimeError("github down")
        return set(names)

    return list_repositories


async def test_github_app_installation_selection_is_the_inferred_github_allowlist(tmp_path, monkeypatch):
    """The repositories the App was installed on are the allowlist of the inferred github.com provider."""
    _write_github_app(tmp_path)
    monkeypatch.setattr(
        "greenkube.automation.git.credentials.GitProviderCredentialResolver.resolve_credentials",
        _fake_app_credentials,
    )
    repository = FakeGitOpsRepository([GITHUB_REPO, OTHER_GITHUB_REPO])
    readiness = _readiness(repository, tmp_path)
    calls: list = []
    readiness._app_repositories = _app_selection("Acme/GitOps", "acme/payments", calls=calls)

    await readiness.run_once()
    snapshot = await readiness.run_once()

    assert repository.allowlists["inferred-github.com"] == {"acme/gitops", "acme/payments"}
    states = {repo.repository: repo for repo in snapshot.repositories}
    assert states["acme/gitops"].preview.active
    assert states["acme/legacy"].preview.reason == "github_app_not_installed"
    assert states["acme/legacy"].allowlisted is False
    # The selection is cached between ticks (rate limits).
    assert calls == [("123", None)]


async def test_explicit_allowlist_narrows_the_github_app_selection(tmp_path, monkeypatch):
    _write_github_app(tmp_path)
    monkeypatch.setattr(
        "greenkube.automation.git.credentials.GitProviderCredentialResolver.resolve_credentials",
        _fake_app_credentials,
    )
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path, config=_config(automation_repository_allowlist=("acme/pay*",)))
    readiness._app_repositories = _app_selection("acme/gitops", "acme/payments")

    await readiness.run_once()

    assert repository.allowlists["inferred-github.com"] == {"acme/payments"}


async def test_administrator_allowed_repositories_inside_the_github_app_selection_stay_allowlisted(
    tmp_path, monkeypatch
):
    """A repository an administrator allowed joins a narrowed App allowlist only if the App is installed on it."""
    _write_github_app(tmp_path)
    monkeypatch.setattr(
        "greenkube.automation.git.credentials.GitProviderCredentialResolver.resolve_credentials",
        _fake_app_credentials,
    )
    other = "https://github.com/acme/other.git"
    allowed_at = datetime.now(timezone.utc)
    allowed = [
        AllowedRepository(other, "github.com", "acme/other", "user:admin", allowed_at),
        AllowedRepository(OTHER_GITHUB_REPO, "github.com", "acme/legacy", "user:admin", allowed_at),
    ]
    repository = FakeGitOpsRepository([GITHUB_REPO, other, OTHER_GITHUB_REPO])
    readiness = _readiness(
        repository,
        tmp_path,
        store=FakeStore(allowed),
        config=_config(automation_repository_allowlist=("acme/gitops",)),
    )
    readiness._app_repositories = _app_selection("acme/gitops", "Acme/Other")

    snapshot = await readiness.run_once()

    assert repository.allowlists["inferred-github.com"] == {"acme/gitops", "acme/other"}
    states = {repo.repository: repo for repo in snapshot.repositories}
    assert states["acme/other"].allowlisted is True
    assert states["acme/legacy"].allowlisted is False


async def test_an_unreadable_github_app_selection_keeps_the_last_known_one(tmp_path, monkeypatch):
    _write_github_app(tmp_path)
    monkeypatch.setattr(
        "greenkube.automation.git.credentials.GitProviderCredentialResolver.resolve_credentials",
        _fake_app_credentials,
    )
    clock = [0.0]
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path)
    readiness._monotonic = lambda: clock[0]
    fail: list = []
    readiness._app_repositories = _app_selection("acme/payments", fail=fail)

    await readiness.run_once()
    assert repository.allowlists["inferred-github.com"] == {"acme/payments"}

    clock[0] = 10_000.0
    fail.append(True)
    await readiness.run_once()
    assert repository.allowlists["inferred-github.com"] == {"acme/payments"}


async def _fake_app_credentials(self, provider):
    return SimpleNamespace(app_id="123", private_key="pem")


def _helm_source(repo_url: str, chart: dict, values_sources: list[dict], name: str) -> WorkloadSource:
    return _source(repo_url, name=name).model_copy(
        update={
            "renderer": RendererKind.HELM_VALUES,
            "renderer_config": {"chart": chart, "values_files": [], "values_sources": values_sources},
        }
    )


def test_repositories_that_only_serve_as_a_chart_source_are_never_suggested():
    """F-11: a Helm repository or an upstream Git chart whose values live elsewhere is not suggested."""
    values_repo = "https://gitlab.example.com/ops/values.git"
    helm_repository = _helm_source(
        "https://charts.example.com/stable", {"name": "vpa", "repo": "https://charts.example.com/stable"}, [], "app-0"
    )
    upstream_git_chart = _helm_source(
        "https://github.com/upstream/silo.git",
        {"name": None, "repo": "https://github.com/upstream/silo.git"},
        [
            {"source_index": 1, "source_ref": "values", "repo_url": values_repo},
            {"source_index": 2, "source_ref": "extra", "repo_url": "https://gitlab.example.com/ops/extra.git"},
        ],
        "app-1",
    )
    local_values_chart = _helm_source(
        GITHUB_REPO,
        {"name": None, "repo": GITHUB_REPO},
        [{"source_index": 0, "source_ref": None, "repo_url": GITHUB_REPO}],
        "app-2",
    )
    bindings = [
        WorkloadBinding(
            cluster="default",
            namespace="payments",
            workload_kind="Deployment",
            workload_name=f"app-{index}",
            status=BindingStatus.BLOCKED,
            confidence=0.1,
            candidates=(source,),
        )
        for index, source in enumerate((helm_repository, upstream_git_chart, local_values_chart))
    ]
    bindings.append(_binding(values_repo))

    assert _repository_urls(bindings) == [GITHUB_REPO, values_repo]


def test_a_monorepo_chart_whose_values_spell_the_repository_differently_is_still_suggested():
    """One repository written as ``.../gitops.git`` and ``git@host:.../gitops`` is not a chart-only repository."""
    chart_repo = "https://gitlab.example.com/ops/gitops.git"
    monorepo_chart = _helm_source(
        chart_repo,
        {"name": None, "repo": chart_repo},
        [{"source_index": 1, "source_ref": "values", "repo_url": "git@gitlab.example.com:ops/gitops"}],
        "app-0",
    )
    binding = WorkloadBinding(
        cluster="default",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="app-0",
        status=BindingStatus.BLOCKED,
        confidence=0.1,
        candidates=(monorepo_chart,),
    )

    assert _repository_urls([binding]) == [chart_repo]
