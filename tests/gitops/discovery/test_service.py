import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from greenkube.gitops.bindings.manual import prepare_manual_binding_resolution
from greenkube.gitops.bindings.resolver import WorkloadBindingResolver
from greenkube.gitops.contracts import (
    BindingStatus,
    ControllerKind,
    RendererKind,
    WorkloadSource,
    binding_candidate_generation_token,
)
from greenkube.gitops.discovery.kubernetes import (
    CapabilityAvailability,
    KubernetesDiscoveryResult,
    KubernetesDiscoverySource,
)
from greenkube.gitops.discovery.service import (
    DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    DiscoveryCoverage,
    GitOpsDiscoveryService,
)
from greenkube.storage.base_gitops_repository import WorkloadBinding, WorkloadBindingRepository

_NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
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


def _source(
    source_id: str = "source-1",
    *,
    repo_url: str = "https://github.com/acme/gitops.git",
    controller_generation: int = 7,
    source_generation: int = 3,
    observed_at: datetime = _NOW,
    controller_kind: ControllerKind = ControllerKind.ARGOCD,
) -> WorkloadSource:
    return WorkloadSource(
        id=source_id,
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url=repo_url,
        ref="main",
        revision="a" * 40,
        path="apps/payments",
        controller_kind=controller_kind,
        controller_name="payments",
        controller_namespace="gitops-system",
        controller_uid="application-uid",
        controller_generation=controller_generation,
        source_uid="repository-uid",
        source_generation=source_generation,
        source_revision="a" * 40,
        renderer=RendererKind.RAW,
        confidence=0.95,
        status=BindingStatus.RESOLVED,
        evidence={"resource": "Deployment/api"},
        observed_at=observed_at,
    )


def _availability(**overrides: bool) -> dict[str, CapabilityAvailability]:
    return {name: CapabilityAvailability(available=overrides.get(name, True)) for name in _CAPABILITIES}


def _result(
    candidates: Iterable[WorkloadSource] = (),
    *,
    availability: Mapping[str, CapabilityAvailability] | None = None,
) -> KubernetesDiscoveryResult:
    return KubernetesDiscoveryResult(
        candidates=list(candidates),
        availability=dict(availability) if availability is not None else _availability(),
    )


def _binding(source: WorkloadSource) -> WorkloadBinding:
    return WorkloadBindingResolver().resolve([source], now=_NOW)


class _MemoryBindingRepository(WorkloadBindingRepository):
    def __init__(self, bindings: Iterable[WorkloadBinding] = ()) -> None:
        self.bindings = {_identity(binding): binding for binding in bindings}
        self.save_count = 0
        self.events = []

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

    async def record_workload_binding_event(self, event):
        if all(existing.event_key != event.event_key for existing in self.events):
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
        if current is None or current.status not in {BindingStatus.AMBIGUOUS, BindingStatus.UNRESOLVED}:
            raise ValueError("current binding is no longer actionable")
        candidate = next((source for source in current.candidates if source.id == expected_candidate_id), None)
        if candidate is None or binding_candidate_generation_token(candidate) != expected_generation_token:
            raise ValueError("current binding candidate changed")
        return await self.save_workload_binding_with_event(binding, event)


def _identity(binding: WorkloadBinding) -> tuple[str, str, str, str]:
    return binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name


class _DiscoveryStub:
    def __init__(self, result: KubernetesDiscoveryResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

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
        return self.result


class _CustomObjectsApi:
    def __init__(self, responses: Mapping[str, object] | None = None) -> None:
        self.responses = dict(responses or {})

    async def list_cluster_custom_object(self, group: str, version: str, plural: str) -> object:
        response = self.responses.get(plural, {"items": []})
        if isinstance(response, Exception):
            raise response
        return response

    async def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
    ) -> object:
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
            "destination": {"namespace": "payments"},
            "source": {
                "repoURL": "https://github.com/acme/gitops.git",
                "targetRevision": "main",
                "path": "apps/payments",
            },
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


@pytest.mark.asyncio
async def test_refresh_persists_adapter_candidates_and_exposes_coverage_idempotently():
    discovery = KubernetesDiscoverySource(_CustomObjectsApi({"applications": {"items": [_application()]}}))
    repository = _MemoryBindingRepository()
    service = GitOpsDiscoveryService(discovery, repository, cluster="prod", provider_id="github-main")

    first = await service.refresh()
    second = await service.refresh()

    assert first.coverage.total == 1
    assert first.coverage.resolved == 1
    assert first.coverage.ratio == 1.0
    assert first.availability["argocd"].available is True
    assert len(first.bindings) == len(second.bindings) == 1
    assert first.bindings[0].status is BindingStatus.RESOLVED
    assert first.bindings[0].source is not None
    assert first.bindings[0].source.controller_kind is ControllerKind.ARGOCD
    assert len(first.bindings[0].candidates) == 1
    assert len(repository.bindings) == 1
    assert repository.save_count == 2


@pytest.mark.asyncio
async def test_refresh_forwards_scope_and_live_resources_to_injected_discovery_source():
    discovery = _DiscoveryStub(_result())
    repository = _MemoryBindingRepository()
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        namespace="payments",
    )
    resources = [{"kind": "Deployment", "metadata": {"name": "api"}}]

    await service.refresh(resources=resources)

    assert discovery.calls == [
        {
            "cluster": "prod",
            "provider_id": "github-main",
            "namespace": "payments",
            "resources": tuple(resources),
        }
    ]


@pytest.mark.asyncio
async def test_refresh_preserves_a_manual_resolution_when_its_source_generation_is_current():
    source = _source()
    competing = _source("source-2", repo_url="https://github.com/acme/other.git")
    unresolved = WorkloadBindingResolver().resolve([source, competing], now=_NOW)
    prepared = prepare_manual_binding_resolution(
        unresolved,
        candidate_id=source.id,
        generation_token=binding_candidate_generation_token(source),
        actor="operator",
        now=_NOW,
    )
    repository = _MemoryBindingRepository([prepared.binding])
    repository.events.append(prepared.event)
    discovery = _DiscoveryStub(_result([source, competing]))
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        clock=lambda: _NOW + timedelta(minutes=1),
    )

    result = await service.refresh()

    assert result.bindings[0].status is BindingStatus.RESOLVED
    assert result.bindings[0].source is not None
    assert result.bindings[0].source.controller_kind is ControllerKind.ARGOCD
    assert result.bindings[0].source.evidence["selected_via"] == "manual"
    assert result.bindings[0].source.evidence["manual_resolution"]["generation_token"] == (
        binding_candidate_generation_token(source)
    )
    assert [event.event_type for event in repository.events] == ["manual_resolution_created"]


@pytest.mark.asyncio
async def test_refresh_cannot_overwrite_a_manual_resolution_committed_after_its_read():
    source = _source()
    competing = _source("source-2", repo_url="https://github.com/acme/other.git")
    unresolved = WorkloadBindingResolver().resolve([source, competing], now=_NOW)

    class ConcurrentManualResolutionRepository(_MemoryBindingRepository):
        resolved_during_save = False

        async def save_discovered_workload_binding(self, binding, *, expected_generation, events=()):
            if not self.resolved_during_save:
                self.resolved_during_save = True
                current = await self.get_workload_binding(*_identity(binding))
                assert current is not None
                prepared = prepare_manual_binding_resolution(
                    current,
                    candidate_id=source.id,
                    generation_token=binding_candidate_generation_token(source),
                    actor="operator",
                    now=_NOW + timedelta(minutes=1),
                )
                await self.save_manual_workload_binding(
                    prepared.binding,
                    prepared.event,
                    expected_candidate_id=prepared.selected_candidate_id,
                    expected_generation_token=prepared.generation_token,
                    now=prepared.event.created_at,
                )
            return await super().save_discovered_workload_binding(
                binding,
                expected_generation=expected_generation,
                events=events,
            )

    repository = ConcurrentManualResolutionRepository([unresolved])
    discovery = _DiscoveryStub(_result([source, competing]))
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        clock=lambda: _NOW + timedelta(minutes=2),
    )

    result = await service.refresh()

    binding = result.bindings[0]
    assert binding.status is BindingStatus.RESOLVED
    assert binding.source is not None
    assert binding.source.evidence["selected_via"] == "manual"
    assert repository.events[-1].event_type == "manual_resolution_created"


@pytest.mark.asyncio
async def test_refresh_invalidates_and_audits_manual_resolution_when_source_generation_changes():
    source = _source()
    competing = _source("source-2", repo_url="https://github.com/acme/other.git")
    unresolved = WorkloadBindingResolver().resolve([source, competing], now=_NOW)
    prepared = prepare_manual_binding_resolution(
        unresolved,
        candidate_id=source.id,
        generation_token=binding_candidate_generation_token(source),
        actor="operator",
        now=_NOW,
    )
    repository = _MemoryBindingRepository([prepared.binding])
    repository.events.append(prepared.event)
    manual_source = prepared.binding.source
    assert manual_source is not None
    changed = source.model_copy(update={"controller_generation": 8, "observed_at": _NOW + timedelta(minutes=1)})
    discovery = _DiscoveryStub(_result([changed, competing]))
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        clock=lambda: _NOW + timedelta(minutes=1),
    )

    first = await service.refresh()
    second = await service.refresh()

    assert first.bindings[0].status is BindingStatus.AMBIGUOUS
    refreshed_source = next(candidate for candidate in first.bindings[0].candidates if candidate.id == source.id)
    assert refreshed_source.status is BindingStatus.RESOLVED
    assert refreshed_source.controller_generation == 8
    assert all(candidate.evidence.get("selected_via") != "manual" for candidate in first.bindings[0].candidates)
    assert [event.event_type for event in repository.events] == [
        "manual_resolution_created",
        "manual_resolution_invalidated",
    ]
    assert repository.events[-1].details["reason_code"] == "manual-resolution-source-changed"
    assert second.bindings[0].status is BindingStatus.AMBIGUOUS
    assert len(repository.events) == 2


@pytest.mark.asyncio
async def test_refresh_expires_and_audits_manual_resolution_once():
    source = _source()
    competing = _source("source-2", repo_url="https://github.com/acme/other.git")
    unresolved = WorkloadBindingResolver().resolve([source, competing], now=_NOW)
    prepared = prepare_manual_binding_resolution(
        unresolved,
        candidate_id=source.id,
        generation_token=binding_candidate_generation_token(source),
        actor="operator",
        now=_NOW,
        ttl=timedelta(seconds=10),
    )
    repository = _MemoryBindingRepository([prepared.binding])
    repository.events.append(prepared.event)
    discovery = _DiscoveryStub(_result([source, competing]))
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        clock=lambda: _NOW + timedelta(seconds=11),
    )

    first = await service.refresh()
    second = await service.refresh()

    assert first.bindings[0].status is BindingStatus.AMBIGUOUS
    assert [event.event_type for event in repository.events] == [
        "manual_resolution_created",
        "manual_resolution_expired",
    ]
    assert second.bindings[0].status is BindingStatus.AMBIGUOUS
    assert len(repository.events) == 2


@pytest.mark.asyncio
async def test_refresh_marks_vanished_bindings_stale_and_retains_candidate_history():
    original = _binding(_source())
    discovery = _DiscoveryStub(_result())
    repository = _MemoryBindingRepository([original])
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        clock=lambda: _NOW + timedelta(minutes=1),
    )

    first = await service.refresh()
    second = await service.refresh()

    assert len(first.bindings) == len(second.bindings) == 1
    stale = second.bindings[0]
    assert stale.status is BindingStatus.STALE
    assert stale.source is not None
    assert stale.source.id == "source-1"
    assert stale.source.status is BindingStatus.STALE
    assert [candidate.id for candidate in stale.candidates] == ["source-1"]
    assert stale.candidates[0].status is BindingStatus.STALE
    assert len(repository.bindings) == 1


@pytest.mark.asyncio
async def test_missing_crd_is_reported_as_capability_metadata_not_as_a_vanished_source():
    existing = _binding(_source())
    responses = {
        "applications": ApiException(status=404, reason="Not Found"),
    }
    discovery = KubernetesDiscoverySource(_CustomObjectsApi(responses))
    repository = _MemoryBindingRepository([existing])
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        clock=lambda: _NOW + timedelta(minutes=1),
    )

    result = await service.refresh()

    assert result.availability["argocd"].available is False
    assert result.availability["argocd"].reason == "crd-not-found"
    assert result.bindings[0].status is BindingStatus.RESOLVED
    assert result.bindings[0].source is not None
    assert result.bindings[0].source.id == "source-1"


@pytest.mark.asyncio
async def test_namespace_scoped_refresh_does_not_stale_bindings_from_unlisted_namespaces():
    existing = _binding(_source())
    discovery = _DiscoveryStub(_result())
    repository = _MemoryBindingRepository([existing])
    service = GitOpsDiscoveryService(
        discovery,
        repository,
        cluster="prod",
        provider_id="github-main",
        namespace="payments",
        clock=lambda: _NOW + timedelta(minutes=1),
    )

    result = await service.refresh()

    assert result.bindings[0].status is BindingStatus.RESOLVED
    assert result.bindings[0].source is not None
    assert result.bindings[0].source.id == "source-1"


@pytest.mark.asyncio
async def test_discovery_rbac_errors_propagate_from_refresh():
    discovery = KubernetesDiscoverySource(
        _CustomObjectsApi({"applications": ApiException(status=403, reason="Forbidden")})
    )
    service = GitOpsDiscoveryService(
        discovery,
        _MemoryBindingRepository(),
        cluster="prod",
        provider_id="github-main",
    )

    with pytest.raises(ApiException) as error:
        await service.refresh()

    assert error.value.status == 403


@pytest.mark.asyncio
async def test_coverage_counts_each_binding_status():
    statuses = (
        BindingStatus.RESOLVED,
        BindingStatus.AMBIGUOUS,
        BindingStatus.UNRESOLVED,
        BindingStatus.BLOCKED,
        BindingStatus.STALE,
    )
    bindings = [
        WorkloadBinding(
            cluster="prod",
            namespace="payments",
            workload_kind="Deployment",
            workload_name=f"workload-{index}",
            status=status,
            confidence=0.5,
        )
        for index, status in enumerate(statuses)
    ]
    service = GitOpsDiscoveryService(
        _DiscoveryStub(_result()),
        _MemoryBindingRepository(bindings),
        cluster="prod",
        provider_id="github-main",
    )

    coverage = await service.coverage()

    assert coverage == DiscoveryCoverage(total=5, resolved=1, ambiguous=1, unresolved=1, blocked=1, stale=1)
    assert coverage.ratio == 0.2


@pytest.mark.asyncio
async def test_periodic_refresh_stops_when_its_cancellation_event_is_set():
    stop_event = asyncio.Event()

    class _StoppingDiscovery(_DiscoveryStub):
        async def discover(
            self,
            *,
            cluster: str,
            provider_id: str,
            namespace: str | None = None,
            resources: Iterable[Mapping[str, Any]] = (),
        ) -> KubernetesDiscoveryResult:
            result = await super().discover(
                cluster=cluster,
                provider_id=provider_id,
                namespace=namespace,
                resources=resources,
            )
            stop_event.set()
            return result

    discovery = _StoppingDiscovery(_result())
    service = GitOpsDiscoveryService(
        discovery,
        _MemoryBindingRepository(),
        cluster="prod",
        provider_id="github-main",
    )

    await asyncio.wait_for(
        service.run_periodic(stop_event=stop_event, interval_seconds=DEFAULT_DISCOVERY_INTERVAL_SECONDS),
        timeout=0.5,
    )

    assert len(discovery.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [0, -1, float("inf"), float("nan"), 3601])
async def test_periodic_refresh_rejects_intervals_outside_the_supported_bounds(interval_seconds: float):
    discovery = _DiscoveryStub(_result())
    service = GitOpsDiscoveryService(
        discovery,
        _MemoryBindingRepository(),
        cluster="prod",
        provider_id="github-main",
    )

    with pytest.raises(ValueError, match="interval"):
        await service.run_periodic(stop_event=asyncio.Event(), interval_seconds=interval_seconds)

    assert discovery.calls == []
