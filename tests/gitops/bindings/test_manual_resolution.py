from datetime import datetime, timedelta, timezone

import pytest

from greenkube.gitops.bindings.manual import (
    ManualResolutionCandidateError,
    ManualResolutionConflictError,
    ManualResolutionStaleGenerationError,
    prepare_manual_binding_resolution,
)
from greenkube.gitops.bindings.resolver import DEFAULT_BINDING_TTL, WorkloadBindingResolver
from greenkube.gitops.contracts import (
    BindingStatus,
    ControllerKind,
    RendererKind,
    WorkloadSource,
    binding_candidate_generation_token,
)

_NOW = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)


def _source(
    source_id: str,
    *,
    repo_url: str,
    controller_generation: int = 7,
    source_generation: int = 3,
    status: BindingStatus = BindingStatus.RESOLVED,
    observed_at: datetime = _NOW,
    expires_at: datetime | None = None,
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
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        controller_uid="application-uid",
        controller_generation=controller_generation,
        source_uid="repository-uid",
        source_generation=source_generation,
        source_revision="b" * 40,
        renderer=RendererKind.RAW,
        renderer_config={"owner": {"name": "payments"}},
        confidence=0.95,
        status=status,
        evidence={"resource": {"kind": "Deployment", "name": "api"}},
        observed_at=observed_at,
        expires_at=expires_at,
    )


def _ambiguous_binding(*, candidates: tuple[WorkloadSource, ...] | None = None):
    sources = candidates or (
        _source("argo-source", repo_url="https://github.example.test/acme/one.git"),
        _source("flux-source", repo_url="https://github.example.test/acme/two.git"),
    )
    return WorkloadBindingResolver().resolve(sources, now=_NOW)


def test_manual_resolution_persists_exact_source_generation_with_a_bounded_expiry():
    binding = _ambiguous_binding()
    candidate = binding.candidates[0]
    generation_token = binding_candidate_generation_token(candidate)

    prepared = prepare_manual_binding_resolution(
        binding,
        candidate_id=candidate.id,
        generation_token=generation_token,
        actor="operator@example.test",
        now=_NOW,
    )

    manual = prepared.binding.source
    assert prepared.binding.status is BindingStatus.RESOLVED
    assert manual is not None
    assert manual.id == candidate.id
    assert manual.controller_kind is candidate.controller_kind
    assert manual.provider_id == candidate.provider_id
    assert manual.repo_url == candidate.repo_url
    assert manual.ref == candidate.ref
    assert manual.revision == candidate.revision
    assert manual.path == candidate.path
    assert manual.controller_name == candidate.controller_name
    assert manual.controller_namespace == candidate.controller_namespace
    assert manual.renderer_config == candidate.renderer_config
    assert manual.renderer is candidate.renderer
    assert manual.controller_uid == candidate.controller_uid
    assert manual.controller_generation == candidate.controller_generation
    assert manual.source_uid == candidate.source_uid
    assert manual.source_generation == candidate.source_generation
    assert manual.source_revision == candidate.source_revision
    assert manual.expires_at == _NOW + DEFAULT_BINDING_TTL
    assert manual.evidence["selected_via"] == "manual"
    assert manual.evidence["resource"] == candidate.evidence["resource"]
    snapshot = manual.evidence["manual_resolution"]
    assert snapshot["selected_candidate_id"] == candidate.id
    assert snapshot["generation_token"] == generation_token
    assert snapshot["controller_generation"] == candidate.controller_generation
    assert snapshot["source_generation"] == candidate.source_generation
    assert prepared.event.event_type == "manual_resolution_created"
    assert prepared.event.source_generation == generation_token
    assert prepared.event.actor == "operator@example.test"
    assert prepared.event.expires_at == manual.expires_at


def test_manual_resolution_preserves_flux_controller_and_source_evidence():
    candidate = _source(
        "flux-source",
        repo_url="https://github.example.test/acme/one.git",
    ).model_copy(
        update={
            "controller_kind": ControllerKind.FLUX_HELM_RELEASE,
            "evidence": {
                "source_ref": {"kind": "GitRepository", "name": "gitops", "namespace": "flux-system"},
                "artifact_revision": f"main@sha1:{'b' * 40}",
                "resource": {"kind": "Deployment", "name": "api"},
            },
        }
    )
    competing = _source("other-source", repo_url="https://github.example.test/acme/two.git").model_copy(
        update={"controller_kind": ControllerKind.FLUX_HELM_RELEASE}
    )
    binding = _ambiguous_binding(candidates=(candidate, competing))

    prepared = prepare_manual_binding_resolution(
        binding,
        candidate_id=candidate.id,
        generation_token=binding_candidate_generation_token(candidate),
        actor="operator",
        now=_NOW,
    )

    manual = prepared.binding.source
    assert manual is not None
    assert manual.controller_kind is ControllerKind.FLUX_HELM_RELEASE
    assert manual.evidence["selected_via"] == "manual"
    assert manual.evidence["source_ref"] == candidate.evidence["source_ref"]
    assert manual.evidence["artifact_revision"] == candidate.evidence["artifact_revision"]
    assert manual.evidence["manual_resolution"]["controller_kind"] == ControllerKind.FLUX_HELM_RELEASE.value


def test_manual_resolution_rejects_candidate_ids_not_in_the_persisted_candidate_set():
    binding = _ambiguous_binding()

    with pytest.raises(ManualResolutionCandidateError):
        prepare_manual_binding_resolution(
            binding,
            candidate_id="client-invented",
            generation_token=f"sha256:{'a' * 64}",
            actor="operator",
            now=_NOW,
        )


def test_manual_resolution_rejects_stale_source_generation_tokens():
    binding = _ambiguous_binding()
    candidate = binding.candidates[0]

    with pytest.raises(ManualResolutionStaleGenerationError):
        prepare_manual_binding_resolution(
            binding,
            candidate_id=candidate.id,
            generation_token=f"sha256:{'0' * 64}",
            actor="operator",
            now=_NOW,
        )


def test_manual_resolution_ttl_cannot_exceed_the_binding_default():
    binding = _ambiguous_binding()
    candidate = binding.candidates[0]

    with pytest.raises(ValueError, match="no greater than the binding TTL"):
        prepare_manual_binding_resolution(
            binding,
            candidate_id=candidate.id,
            generation_token=binding_candidate_generation_token(candidate),
            actor="operator",
            now=_NOW,
            ttl=DEFAULT_BINDING_TTL + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "binding",
    [
        WorkloadBindingResolver().resolve(
            [_source("resolved", repo_url="https://github.example.test/acme/one.git")],
            now=_NOW,
        ),
        WorkloadBindingResolver().resolve(
            [
                _source(
                    "expired",
                    repo_url="https://github.example.test/acme/one.git",
                    expires_at=_NOW - timedelta(seconds=1),
                )
            ],
            now=_NOW,
        ),
    ],
)
def test_manual_resolution_only_accepts_current_actionable_bindings(binding):
    candidate = binding.candidates[0]

    with pytest.raises((ManualResolutionConflictError, ManualResolutionCandidateError)):
        prepare_manual_binding_resolution(
            binding,
            candidate_id=candidate.id,
            generation_token=binding_candidate_generation_token(candidate),
            actor="operator",
            now=_NOW,
        )


def test_generation_token_changes_when_controller_or_source_identity_changes():
    source = _source("source", repo_url="https://github.example.test/acme/one.git")

    assert binding_candidate_generation_token(source) != binding_candidate_generation_token(
        source.model_copy(update={"controller_uid": "replacement-controller"})
    )
    assert binding_candidate_generation_token(source) != binding_candidate_generation_token(
        source.model_copy(update={"controller_generation": 8})
    )
    assert binding_candidate_generation_token(source) != binding_candidate_generation_token(
        source.model_copy(update={"source_uid": "replacement-source"})
    )
    assert binding_candidate_generation_token(source) != binding_candidate_generation_token(
        source.model_copy(update={"source_generation": 4})
    )
    assert binding_candidate_generation_token(source) == binding_candidate_generation_token(
        source.model_copy(
            update={
                "renderer_config": {
                    "owner": {"name": "payments"},
                    "values": {"password": "not-in-the-token"},
                }
            }
        )
    )
    assert binding_candidate_generation_token(source) != binding_candidate_generation_token(
        source.model_copy(
            update={
                "renderer_config": {
                    "owner": {"name": "payments"},
                    "values_mapping": {"file_path": "apps/payments/other-values.yaml"},
                }
            }
        )
    )
    assert binding_candidate_generation_token(source) == binding_candidate_generation_token(
        source.model_copy(
            update={"repo_url": "https://operator:credential@github.example.test/acme/one.git?access_token=hidden"}
        )
    )
