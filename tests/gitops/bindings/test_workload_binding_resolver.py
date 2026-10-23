from datetime import datetime, timedelta, timezone
from typing import Any

from greenkube.gitops.bindings.resolver import WorkloadBindingResolver
from greenkube.gitops.contracts import BindingStatus, ControllerKind, RendererKind, WorkloadSource

_NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def _source(
    source_id: str,
    controller_kind: ControllerKind,
    **overrides: Any,
) -> WorkloadSource:
    values: dict[str, Any] = {
        "id": source_id,
        "cluster": "prod",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "api",
        "provider_id": "github-main",
        "repo_url": "https://github.com/acme/gitops.git",
        "ref": "main",
        "revision": "a" * 40,
        "path": "apps/payments",
        "controller_kind": controller_kind,
        "controller_name": source_id,
        "controller_namespace": "gitops-system",
        "source_revision": "a" * 40,
        "renderer": RendererKind.RAW,
        "confidence": 0.8,
        "status": BindingStatus.RESOLVED,
        "evidence": {"source": source_id},
        "observed_at": _NOW,
    }
    values.update(overrides)
    return WorkloadSource(**values)


def test_higher_source_precedence_wins_and_identical_units_merge_with_provenance():
    argocd = _source("argocd-source", ControllerKind.ARGOCD, confidence=0.6)
    flux_inventory = _source(
        "flux-source",
        ControllerKind.FLUX_KUSTOMIZATION,
        confidence=0.9,
        observed_at=_NOW + timedelta(minutes=1),
    )
    lower_priority = _source(
        "helm-source",
        ControllerKind.FLUX_HELM_RELEASE,
        confidence=1.0,
        repo_url="https://github.com/acme/other.git",
        path="charts/payments",
    )

    binding = WorkloadBindingResolver().resolve(
        [argocd, flux_inventory, lower_priority],
        now=_NOW + timedelta(minutes=2),
    )

    assert binding.status is BindingStatus.RESOLVED
    assert binding.source == flux_inventory
    assert {candidate.id for candidate in binding.candidates} == {
        "argocd-source",
        "flux-source",
        "helm-source",
    }
    assert {item["source_id"] for item in binding.evidence["provenance"]} == {"argocd-source", "flux-source"}


def test_controller_precedence_beats_confidence_from_lower_priority_sources():
    controller = _source("argocd-source", ControllerKind.ARGOCD, confidence=0.25)
    helm_release = _source(
        "helm-source",
        ControllerKind.FLUX_HELM_RELEASE,
        confidence=0.99,
        repo_url="https://github.com/acme/other.git",
    )

    binding = WorkloadBindingResolver().resolve([helm_release, controller], now=_NOW)

    assert binding.status is BindingStatus.RESOLVED
    assert binding.source == controller


def test_manual_resolution_has_precedence_over_controller_candidates():
    manual = _source(
        "manual",
        ControllerKind.MANUAL_RESOLUTION,
        repo_url="https://github.com/acme/approved.git",
    )
    controller = _source("argocd", ControllerKind.ARGOCD)

    binding = WorkloadBindingResolver().resolve([controller, manual], now=_NOW)

    assert binding.status is BindingStatus.RESOLVED
    assert binding.source == manual


def test_manual_selection_has_precedence_without_replacing_controller_kind():
    manual = _source(
        "manual",
        ControllerKind.ARGOCD,
        repo_url="https://github.com/acme/approved.git",
        evidence={"selected_via": "manual"},
    )
    controller = _source("argo", ControllerKind.ARGOCD)

    binding = WorkloadBindingResolver().resolve([controller, manual], now=_NOW)

    assert binding.status is BindingStatus.RESOLVED
    assert binding.source == manual
    assert binding.source is not None
    assert binding.source.controller_kind is ControllerKind.ARGOCD


def test_conflicting_equal_priority_source_units_are_ambiguous_even_with_different_confidence():
    argo = _source("argo", ControllerKind.ARGOCD, confidence=0.99)
    flux = _source(
        "flux",
        ControllerKind.FLUX_KUSTOMIZATION,
        confidence=0.6,
        repo_url="https://github.com/acme/platform-mirror.git",
    )

    binding = WorkloadBindingResolver().resolve([argo, flux], now=_NOW)

    assert binding.status is BindingStatus.AMBIGUOUS
    assert binding.source is None
    assert binding.reason == "conflicting-equal-priority-source-units"
    assert len(binding.evidence["conflicting_source_units"]) == 2


def test_freshness_breaks_ties_between_identical_source_units():
    older = _source(
        "older",
        ControllerKind.ARGOCD,
        confidence=0.8,
        observed_at=_NOW - timedelta(minutes=1),
    )
    newer = _source(
        "newer",
        ControllerKind.FLUX_KUSTOMIZATION,
        confidence=0.8,
        observed_at=_NOW,
    )

    binding = WorkloadBindingResolver().resolve([older, newer], now=_NOW)

    assert binding.status is BindingStatus.RESOLVED
    assert binding.source == newer


def test_confidence_precedes_freshness_for_identical_source_units():
    confident = _source(
        "confident",
        ControllerKind.ARGOCD,
        confidence=0.9,
        observed_at=_NOW - timedelta(minutes=1),
    )
    recent = _source(
        "recent",
        ControllerKind.FLUX_KUSTOMIZATION,
        confidence=0.8,
        observed_at=_NOW,
    )

    binding = WorkloadBindingResolver().resolve([recent, confident], now=_NOW)

    assert binding.status is BindingStatus.RESOLVED
    assert binding.source == confident


def test_expired_high_priority_source_does_not_override_a_fresh_candidate():
    expired = _source(
        "expired-argo",
        ControllerKind.ARGOCD,
        expires_at=_NOW - timedelta(seconds=1),
    )
    current = _source("current-helm", ControllerKind.FLUX_HELM_RELEASE)

    binding = WorkloadBindingResolver().resolve([expired, current], now=_NOW)

    assert binding.status is BindingStatus.RESOLVED
    assert binding.source == current
    assert (
        next(candidate for candidate in binding.candidates if candidate.id == expired.id).status is BindingStatus.STALE
    )


def test_unresolved_high_priority_source_prevents_falling_back_to_lower_priority():
    unresolved = _source(
        "unresolved-argo",
        ControllerKind.ARGOCD,
        status=BindingStatus.UNRESOLVED,
        reason="source-repository-unresolved",
    )
    fallback = _source(
        "resolved-helm",
        ControllerKind.FLUX_HELM_RELEASE,
        repo_url="https://github.com/acme/fallback.git",
    )

    binding = WorkloadBindingResolver().resolve([fallback, unresolved], now=_NOW)

    assert binding.status is BindingStatus.UNRESOLVED
    assert binding.source is None


def test_freshness_ttl_preserves_stale_candidates_but_never_resolves_them():
    old = _source(
        "old-argo",
        ControllerKind.ARGOCD,
        observed_at=_NOW - timedelta(minutes=31),
    )

    binding = WorkloadBindingResolver().resolve([old], now=_NOW)

    assert binding.status is BindingStatus.STALE
    assert binding.source is not None
    assert binding.source.id == old.id
    assert binding.source.status is BindingStatus.STALE
    assert binding.candidates[0].id == old.id
