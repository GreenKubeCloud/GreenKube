"""GitOps discovery and operator-approved binding resolution API tests."""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from kubernetes_asyncio.client.exceptions import ApiException

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_gitops_binding_repository,
    get_gitops_discovery_runtime,
    get_gitops_discovery_source,
)
from greenkube.gitops.bindings.resolver import WorkloadBindingResolver
from greenkube.gitops.contracts import (
    BindingStatus,
    ControllerKind,
    RendererKind,
    WorkloadSource,
    binding_candidate_generation_token,
)
from greenkube.gitops.discovery.kubernetes import CapabilityAvailability, KubernetesDiscoveryResult
from greenkube.storage.base_gitops_repository import WorkloadBinding, WorkloadBindingRepository

_NOW = datetime.now(timezone.utc)
_SECRET = "do-not-return-this-secret"
_CAPABILITIES = (
    "argocd",
    "flux_kustomization",
    "flux_helm_release",
    "flux_source_git_repository",
    "flux_source_oci_repository",
    "flux_source_helm_repository",
    "flux_source_bucket",
)


def _source(
    *,
    workload_name: str = "payments-api",
    source_id: str | None = None,
    status: BindingStatus = BindingStatus.RESOLVED,
    reason: str | None = None,
    observed_at: datetime = _NOW,
    controller_generation: int = 7,
) -> WorkloadSource:
    return WorkloadSource(
        id=source_id or f"source-{workload_name}",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name=workload_name,
        provider_id="github-main",
        repo_url=f"https://user:{_SECRET}@github.example.test/acme/gitops.git?access_token={_SECRET}",
        ref="main",
        revision="a" * 40,
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments-prod",
        controller_namespace="argocd",
        controller_uid="application-uid",
        controller_generation=controller_generation,
        source_revision="b" * 40,
        renderer=RendererKind.RAW,
        renderer_config={"helm_values": {"password": _SECRET}},
        confidence=0.95,
        status=status,
        evidence={"credential": _SECRET, "resource": {"kind": "Deployment", "name": workload_name}},
        observed_at=observed_at,
        reason=reason,
    )


def _binding(source: WorkloadSource) -> WorkloadBinding:
    return WorkloadBindingResolver(freshness_ttl=None).resolve([source], now=source.observed_at)


class _MemoryBindingRepository(WorkloadBindingRepository):
    def __init__(self, bindings: Iterable[WorkloadBinding] = ()) -> None:
        self.bindings = {_identity(binding): binding for binding in bindings}
        self.save_count = 0
        self.events: list[Any] = []
        self.binding_page_calls: list[dict[str, Any]] = []
        self.coverage_calls: list[dict[str, Any]] = []

    async def save_workload_binding(self, binding: WorkloadBinding) -> WorkloadBinding:
        self.save_count += 1
        identity = _identity(binding)
        current = self.bindings.get(identity)
        saved = replace(binding, generation=(current.generation + 1 if current is not None else 1))
        self.bindings[identity] = saved
        return saved

    async def get_workload_binding(
        self,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
    ) -> WorkloadBinding | None:
        return self.bindings.get((cluster, namespace, workload_kind, workload_name))

    async def list_workload_bindings(self, *, cluster: str | None = None) -> list[WorkloadBinding]:
        return [binding for binding in self.bindings.values() if cluster is None or binding.cluster == cluster]

    async def list_workload_bindings_page(self, **kwargs: Any):
        self.binding_page_calls.append(dict(kwargs))
        return await WorkloadBindingRepository.list_workload_bindings_page(self, **kwargs)

    async def get_workload_binding_coverage(self, **kwargs: Any):
        self.coverage_calls.append(dict(kwargs))
        return await WorkloadBindingRepository.get_workload_binding_coverage(self, **kwargs)

    async def record_workload_binding_event(self, event):
        self.events.append(event)
        return event

    async def list_workload_binding_events(
        self,
        *,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        limit: int = 100,
    ):
        return [
            event
            for event in self.events
            if (
                event.cluster,
                event.namespace,
                event.workload_kind,
                event.workload_name,
            )
            == (cluster, namespace, workload_kind, workload_name)
        ][:limit]

    async def save_workload_binding_with_event(self, binding, event):
        saved = await self.save_workload_binding(binding)
        await self.record_workload_binding_event(event)
        return saved

    async def save_manual_workload_binding(
        self,
        binding,
        event,
        *,
        expected_candidate_id: str,
        expected_generation_token: str,
        now: datetime,
    ):
        current = await self.get_workload_binding(
            binding.cluster,
            binding.namespace,
            binding.workload_kind,
            binding.workload_name,
        )
        if current is None or not any(candidate.id == expected_candidate_id for candidate in current.candidates):
            raise ValueError("current binding candidate changed")
        if current.status not in {BindingStatus.AMBIGUOUS, BindingStatus.UNRESOLVED}:
            raise ValueError("current binding is no longer actionable")
        return await self.save_workload_binding_with_event(binding, event)


def _identity(binding: WorkloadBinding) -> tuple[str, str, str, str]:
    return binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name


def _fresh_ambiguous_binding(repository: _MemoryBindingRepository, workload_name: str = "manual-api"):
    observed_at = datetime.now(timezone.utc)
    source = _source(
        workload_name=workload_name,
        status=BindingStatus.AMBIGUOUS,
        observed_at=observed_at,
    )
    binding = _binding(source)
    repository.bindings[_identity(binding)] = binding
    return binding


class _DiscoveryStub:
    def __init__(
        self,
        *,
        availability: Mapping[str, CapabilityAvailability] | None = None,
        error: Exception | None = None,
    ) -> None:
        reported = dict(availability or {name: CapabilityAvailability(available=True) for name in _CAPABILITIES})
        self.result = KubernetesDiscoveryResult(candidates=[], availability=reported)
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.capability_calls: list[dict[str, Any]] = []

    async def check_availability(self, *, namespace: str | None = None):
        self.capability_calls.append({"namespace": namespace})
        if self.error is not None:
            raise self.error
        return dict(self.result.availability)

    async def discover(
        self,
        *,
        cluster: str,
        provider_id: str,
        namespace: str | None = None,
        resources: Iterable[Mapping[str, Any]] = (),
    ) -> KubernetesDiscoveryResult:
        self.calls.append(
            {
                "cluster": cluster,
                "provider_id": provider_id,
                "namespace": namespace,
                "resources": tuple(resources),
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def gitops_client():
    bindings = [
        _binding(_source()),
        _binding(
            _source(
                workload_name="ambiguous-api",
                status=BindingStatus.AMBIGUOUS,
                observed_at=_NOW + timedelta(minutes=1),
            )
        ),
        _binding(
            _source(
                workload_name="blocked-api",
                status=BindingStatus.BLOCKED,
                reason="unsafe-source-path",
                observed_at=_NOW + timedelta(minutes=2),
            )
        ),
        _binding(
            _source(
                workload_name="unresolved-api",
                status=BindingStatus.UNRESOLVED,
                reason="source-url-unresolved",
                observed_at=_NOW + timedelta(minutes=3),
            )
        ),
        _binding(
            _source(
                workload_name="stale-api",
                status=BindingStatus.STALE,
                reason="source-observation-expired",
                observed_at=_NOW + timedelta(minutes=4),
            )
        ),
    ]
    repository = _MemoryBindingRepository(bindings)
    discovery = _DiscoveryStub(
        availability={
            **{name: CapabilityAvailability(available=True) for name in _CAPABILITIES},
            "flux_helm_release": CapabilityAvailability(available=False, reason="crd-not-found"),
        }
    )
    app = create_app()
    app.dependency_overrides[get_gitops_binding_repository] = lambda: repository
    app.dependency_overrides[get_gitops_discovery_source] = lambda: discovery
    with TestClient(app) as client:
        yield client, repository, discovery
    app.dependency_overrides.clear()


def test_status_reports_capabilities_coverage_and_latest_observation_without_writing(gitops_client):
    client, repository, discovery = gitops_client

    response = client.get("/api/v1/gitops/status?cluster=prod")

    assert response.status_code == 200
    body = response.json()
    assert body["cluster"] == "prod"
    assert set(body["discovery"]) == set(_CAPABILITIES)
    assert body["discovery"]["argocd"] == {"available": True, "reason": None}
    assert body["discovery"]["flux_helm_release"] == {"available": False, "reason": "crd-not-found"}
    assert body["coverage"] == {
        "total": 5,
        "resolved": 1,
        "ambiguous": 1,
        "unresolved": 1,
        "blocked": 1,
        "stale": 1,
        "ratio": pytest.approx(1 / 5),
    }
    assert body["last_refresh"] == (_NOW + timedelta(minutes=4)).isoformat().replace("+00:00", "Z")
    assert discovery.calls == []
    assert discovery.capability_calls == [{"namespace": None}]
    assert repository.coverage_calls[0]["cluster"] == "prod"
    assert repository.save_count == 0


def test_manual_discovery_refresh_runs_once_and_returns_persisted_coverage(gitops_client):
    client, _, _ = gitops_client

    class RefreshRuntime:
        calls = 0

        async def refresh(self):
            self.calls += 1
            return SimpleNamespace(
                refreshed_at=_NOW,
                coverage=SimpleNamespace(
                    total=1,
                    resolved=1,
                    ambiguous=0,
                    unresolved=0,
                    blocked=0,
                    stale=0,
                    ratio=1.0,
                ),
            )

    runtime = RefreshRuntime()
    client.app.dependency_overrides[get_gitops_discovery_runtime] = lambda: runtime
    try:
        response = client.post("/api/v1/gitops/refresh?cluster=prod")
    finally:
        client.app.dependency_overrides.pop(get_gitops_discovery_runtime, None)

    assert response.status_code == 200
    assert response.json() == {
        "cluster": "prod",
        "refreshed_at": _NOW.isoformat().replace("+00:00", "Z"),
        "coverage": {
            "total": 1,
            "resolved": 1,
            "ambiguous": 0,
            "unresolved": 0,
            "blocked": 0,
            "stale": 0,
            "ratio": 1.0,
        },
    }
    assert runtime.calls == 1


def test_binding_returns_candidates_reason_and_safe_provenance(gitops_client):
    client, _, _ = gitops_client

    response = client.get("/api/v1/gitops/bindings/payments/Deployment/payments-api?cluster=prod")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["binding"]["controller"] == "argocd/payments-prod"
    assert body["binding"]["repo_url"] == "https://github.example.test/acme/gitops.git"
    assert body["binding"]["path"] == "apps/payments"
    assert body["binding"]["renderer"] == "raw"
    assert body["reason"] is None
    assert body["candidates"][0]["id"] == "source-payments-api"
    assert body["provenance"][0]["source_id"] == "source-payments-api"
    assert body["provenance"][0]["controller_namespace"] == "argocd"
    response_text = response.text
    assert _SECRET not in response_text
    assert "renderer_config" not in body["binding"]
    assert "evidence" not in body["binding"]
    assert "application-uid" not in response_text
    assert "repository-uid" not in response_text


def test_actionable_binding_list_is_bounded_and_contains_safe_generation_tokens(gitops_client):
    client, repository, _ = gitops_client
    _fresh_ambiguous_binding(repository)

    response = client.get("/api/v1/gitops/bindings?cluster=prod&status=ambiguous&status=unresolved&limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 1
    assert isinstance(body["has_more"], bool)
    assert len(body["items"]) <= 1
    assert body["next_cursor"] is not None
    assert repository.binding_page_calls[0]["statuses"] == [BindingStatus.AMBIGUOUS, BindingStatus.UNRESOLVED]
    candidate = body["items"][0]["candidates"][0]
    assert candidate["generation_token"].startswith("sha256:")
    assert _SECRET not in response.text
    assert "renderer_config" not in response.text
    assert "evidence" not in response.text


def test_binding_list_uses_cursor_pages_in_stable_identity_order(gitops_client):
    client, repository, _ = gitops_client
    for name in ("page-a", "page-b", "page-c"):
        binding = _binding(_source(workload_name=name, observed_at=_NOW))
        repository.bindings[_identity(binding)] = binding

    first = client.get("/api/v1/gitops/bindings?cluster=prod&limit=2")
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 2
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second = client.get(
        "/api/v1/gitops/bindings",
        params={"cluster": "prod", "limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()
    first_names = [item["workload_name"] for item in first_body["items"]]
    second_names = [item["workload_name"] for item in second_body["items"]]
    assert set(first_names).isdisjoint(second_names)
    assert first_names == sorted(first_names)
    assert second_names == sorted(second_names)
    assert repository.binding_page_calls[-1]["cursor"] == first_body["next_cursor"]


def test_binding_list_rejects_malformed_or_cross_cluster_cursors(gitops_client):
    client, _, _ = gitops_client

    malformed = client.get("/api/v1/gitops/bindings?cluster=prod&cursor=not-a-cursor")
    valid = client.get("/api/v1/gitops/bindings?cluster=prod&limit=1").json()["next_cursor"]
    mismatched = client.get(
        "/api/v1/gitops/bindings",
        params={"cluster": "other", "cursor": valid},
    )

    assert malformed.status_code == 422
    assert mismatched.status_code == 422
    assert malformed.json()["detail"] == "Invalid GitOps binding cursor."


def test_expired_bindings_are_filtered_and_counted_as_stale_on_read(gitops_client):
    client, repository, _ = gitops_client
    expired_source = _source(workload_name="expired-api", observed_at=_NOW - timedelta(hours=1))
    expired = _binding(expired_source)
    repository.bindings[_identity(expired)] = expired

    detail = client.get("/api/v1/gitops/bindings/payments/Deployment/expired-api?cluster=prod")
    stale_page = client.get("/api/v1/gitops/bindings?cluster=prod&status=stale&limit=100")
    resolved_page = client.get("/api/v1/gitops/bindings?cluster=prod&status=resolved&limit=100")
    blocked_page = client.get("/api/v1/gitops/bindings?cluster=prod&status=blocked&limit=100")
    coverage = client.get("/api/v1/gitops/coverage?cluster=prod")

    assert detail.status_code == 200
    assert detail.json()["status"] == "stale"
    assert detail.json()["binding"]["status"] == "stale"
    assert any(item["workload_name"] == "expired-api" for item in stale_page.json()["items"])
    assert all(item["workload_name"] != "expired-api" for item in resolved_page.json()["items"])
    blocked = next(item for item in blocked_page.json()["items"] if item["workload_name"] == "blocked-api")
    assert blocked["status"] == "blocked"
    assert coverage.json()["stale"] == 2


def test_binding_candidate_projection_is_bounded_for_large_candidate_sets(gitops_client):
    client, repository, _ = gitops_client
    observed_at = datetime.now(timezone.utc)
    candidates = [
        _source(
            workload_name="wide-api",
            source_id=f"candidate-{index}",
            status=BindingStatus.RESOLVED,
            observed_at=observed_at,
        ).model_copy(update={"repo_url": f"https://github.example.test/acme/repository-{index}.git"})
        for index in range(25)
    ]
    binding = WorkloadBindingResolver().resolve(candidates, now=observed_at)
    repository.bindings[_identity(binding)] = binding

    response = client.get("/api/v1/gitops/bindings?cluster=prod&status=ambiguous&limit=100")

    assert response.status_code == 200
    wide = next(item for item in response.json()["items"] if item["workload_name"] == "wide-api")
    assert len(wide["candidates"]) == 20
    assert wide["candidates_truncated"] is True


def test_manual_resolution_uses_only_a_current_server_candidate_and_audits_creation(gitops_client):
    client, repository, _ = gitops_client
    binding = _fresh_ambiguous_binding(repository)
    candidate = binding.candidates[0]
    detail = client.get(
        f"/api/v1/gitops/bindings/{binding.namespace}/{binding.workload_kind}/{binding.workload_name}?cluster=prod"
    )
    generation_token = detail.json()["candidates"][0]["generation_token"]

    response = client.post(
        f"/api/v1/gitops/bindings/{binding.namespace}/{binding.workload_kind}/{binding.workload_name}/resolve?cluster=prod",
        json={
            "candidate_id": candidate.id,
            "generation_token": generation_token,
            "confirmed": True,
        },
        headers={"X-Audit-Actor": "operator@example.test"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["binding"]["controller_kind"] == "argocd"
    assert body["binding"]["repo_url"] == "https://github.example.test/acme/gitops.git"
    assert body["expires_at"] is not None
    assert len(repository.events) == 1
    event = repository.events[0]
    assert event.event_type == "manual_resolution_created"
    assert event.actor == "anonymous"
    assert event.source_generation == generation_token
    assert _SECRET not in response.text
    assert "renderer_config" not in response.text
    assert "evidence" not in response.text


@pytest.mark.parametrize(
    ("candidate_id", "generation_token"),
    [
        ("not-a-server-candidate", f"sha256:{'a' * 64}"),
        ("source-manual-api", f"sha256:{'0' * 64}"),
    ],
)
def test_manual_resolution_rejects_invalid_candidate_ids_and_stale_generations(
    gitops_client,
    candidate_id,
    generation_token,
):
    client, repository, _ = gitops_client
    binding = _fresh_ambiguous_binding(repository)

    response = client.post(
        f"/api/v1/gitops/bindings/{binding.namespace}/{binding.workload_kind}/{binding.workload_name}/resolve?cluster=prod",
        json={
            "candidate_id": candidate_id,
            "generation_token": generation_token,
            "confirmed": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "GitOps binding changed; refresh candidates before resolving."
    assert repository.save_count == 0
    assert repository.events == []


def test_manual_resolution_rejects_client_supplied_source_authority(gitops_client):
    client, repository, _ = gitops_client
    binding = _fresh_ambiguous_binding(repository)

    response = client.post(
        f"/api/v1/gitops/bindings/{binding.namespace}/{binding.workload_kind}/{binding.workload_name}/resolve?cluster=prod",
        json={
            "candidate_id": binding.candidates[0].id,
            "generation_token": f"sha256:{'a' * 64}",
            "confirmed": True,
            "repo_url": "https://user:submitted-credential@attacker.example/repo.git?token=submitted-credential",
            "provider_id": "attacker",
            "path": "arbitrary",
            "renderer_config": {"command": "unsafe"},
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid manual binding resolution request."}
    assert "submitted-credential" not in response.text
    assert repository.save_count == 0
    assert repository.events == []


def test_manual_resolution_rejects_a_candidate_whose_controller_generation_changed(gitops_client):
    client, repository, _ = gitops_client
    binding = _fresh_ambiguous_binding(repository)
    candidate = binding.candidates[0]
    generation_token = binding_candidate_generation_token(candidate)
    changed = candidate.model_copy(update={"controller_generation": 8})
    repository.bindings[_identity(binding)] = _binding(changed)

    response = client.post(
        f"/api/v1/gitops/bindings/{binding.namespace}/{binding.workload_kind}/{binding.workload_name}/resolve?cluster=prod",
        json={
            "candidate_id": candidate.id,
            "generation_token": generation_token,
            "confirmed": True,
        },
    )

    assert response.status_code == 409
    assert repository.save_count == 0
    assert repository.events == []


def test_manual_resolution_endpoint_uses_the_existing_api_key_middleware(monkeypatch):
    repository = _MemoryBindingRepository()
    binding = _fresh_ambiguous_binding(repository, workload_name="protected-api")
    candidate = binding.candidates[0]
    token = binding_candidate_generation_token(candidate)
    app = create_app()
    app.dependency_overrides[get_gitops_binding_repository] = lambda: repository
    monkeypatch.setattr(
        "greenkube.core.config.get_config",
        lambda: SimpleNamespace(API_KEY="test-api-key", API_AUTH_MODE="api_key", ENVIRONMENT="test"),
    )
    try:
        with TestClient(app) as client:
            path = (
                f"/api/v1/gitops/bindings/{binding.namespace}/{binding.workload_kind}/"
                f"{binding.workload_name}/resolve?cluster=prod"
            )
            payload = {
                "candidate_id": candidate.id,
                "generation_token": token,
                "confirmed": True,
            }

            unauthorized = client.post(path, json=payload)
            authorized = client.post(
                path,
                json=payload,
                headers={"Authorization": "Bearer test-api-key"},
            )
    finally:
        app.dependency_overrides.clear()

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_binding_includes_resolution_reason_and_returns_404_when_missing(gitops_client):
    client, _, _ = gitops_client

    blocked = client.get("/api/v1/gitops/bindings/payments/Deployment/blocked-api?cluster=prod")
    missing = client.get("/api/v1/gitops/bindings/payments/Deployment/missing-api?cluster=prod")

    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"
    assert blocked.json()["reason"] == "unsafe-source-path"
    assert blocked.json()["candidates"][0]["reason"] == "unsafe-source-path"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "GitOps workload binding not found."}


def test_binding_redacts_unrecognized_reason_values(gitops_client):
    client, repository, _ = gitops_client
    sensitive = _binding(_source(workload_name="sensitive-api", status=BindingStatus.BLOCKED, reason=_SECRET))
    repository.bindings[_identity(sensitive)] = sensitive

    response = client.get("/api/v1/gitops/bindings/payments/Deployment/sensitive-api?cluster=prod")

    assert response.status_code == 200
    assert response.json()["reason"] == "redacted"
    assert response.json()["candidates"][0]["reason"] == "redacted"
    assert _SECRET not in response.text


@pytest.mark.parametrize(
    "reason",
    ("invalid-helm-values-mapping", "helm-values-mapping-source-index-required"),
)
def test_binding_preserves_supported_helm_mapping_reasons(gitops_client, reason):
    client, repository, _ = gitops_client
    binding = _binding(_source(workload_name="helm-mapping-api", status=BindingStatus.BLOCKED, reason=reason))
    repository.bindings[_identity(binding)] = binding

    response = client.get("/api/v1/gitops/bindings/payments/Deployment/helm-mapping-api?cluster=prod")

    assert response.status_code == 200
    assert response.json()["reason"] == reason
    assert response.json()["candidates"][0]["reason"] == reason


def test_coverage_reports_all_binding_status_counts(gitops_client):
    client, repository, _ = gitops_client

    response = client.get("/api/v1/gitops/coverage?cluster=prod")

    assert response.status_code == 200
    assert response.json() == {
        "total": 5,
        "resolved": 1,
        "ambiguous": 1,
        "unresolved": 1,
        "blocked": 1,
        "stale": 1,
        "ratio": pytest.approx(1 / 5),
    }
    assert repository.coverage_calls[-1]["cluster"] == "prod"


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ApiException(status=403, reason=_SECRET), 403),
        (ApiException(status=500, reason="Internal Server Error"), 500),
        (ConnectionError("Kubernetes API unavailable"), 503),
    ],
)
def test_status_propagates_discovery_errors(gitops_client, error, status_code):
    client, _, discovery = gitops_client
    discovery.error = error

    response = client.get("/api/v1/gitops/status?cluster=prod")

    assert response.status_code == status_code
    assert _SECRET not in response.text
    assert response.json()["detail"] in {
        "Kubernetes discovery request failed.",
        "Kubernetes discovery is unavailable.",
    }


def test_openapi_is_stable_and_exposes_manual_resolution_routes(gitops_client):
    client, _, _ = gitops_client

    first = client.get("/api/v1/openapi.json").json()
    second = client.get("/api/v1/openapi.json").json()
    gitops_paths = {path for path in first["paths"] if path.startswith("/api/v1/gitops/")}

    assert first == second
    assert gitops_paths == {
        "/api/v1/gitops/status",
        "/api/v1/gitops/refresh",
        "/api/v1/gitops/bindings",
        "/api/v1/gitops/bindings/{namespace}/{workload_kind}/{workload_name}",
        "/api/v1/gitops/bindings/{namespace}/{workload_kind}/{workload_name}/resolve",
        "/api/v1/gitops/coverage",
    }
    assert set(first["paths"]["/api/v1/gitops/bindings"]) == {"get"}
    binding_operation = first["paths"]["/api/v1/gitops/bindings/{namespace}/{workload_kind}/{workload_name}"]["get"]
    assert "404" in binding_operation["responses"]
    resolve_operation = first["paths"]["/api/v1/gitops/bindings/{namespace}/{workload_kind}/{workload_name}/resolve"][
        "post"
    ]
    assert "409" in resolve_operation["responses"]
    assert "200" in first["paths"]["/api/v1/gitops/bindings"]["get"]["responses"]
    status_responses = first["paths"]["/api/v1/gitops/status"]["get"]["responses"]
    assert "200" in status_responses
    assert "default" in status_responses
    assert "200" in first["paths"]["/api/v1/gitops/coverage"]["get"]["responses"]
