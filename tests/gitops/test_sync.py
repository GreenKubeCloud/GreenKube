from collections.abc import Mapping
from copy import deepcopy
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
from greenkube.gitops.sync import (
    GitOpsSyncReason,
    GitOpsSyncStatus,
    observe_gitops_sync,
)

MERGE_SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO_URL = "https://github.com/acme/apps.git"


class _CustomObjectsApi:
    def __init__(
        self,
        response: Mapping[str, Any] | None = None,
        error: Exception | None = None,
        responses_by_plural: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.response = response if response is not None else {"items": [], "metadata": {}}
        self.error = error
        self.responses_by_plural = dict(responses_by_plural or {})
        self.calls: list[dict[str, Any]] = []

    async def list_namespaced_custom_object(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        plural = kwargs.get("plural")
        return self.responses_by_plural.get(plural, self.response) if isinstance(plural, str) else self.response


def _source(
    *,
    controller_kind: ControllerKind = ControllerKind.ARGOCD,
    controller_name: str = "payments",
    controller_namespace: str = "argocd",
    repo_url: str = REPO_URL,
    ref: str = "main",
    evidence: dict[str, Any] | None = None,
) -> WorkloadSource:
    source_evidence = evidence or {}
    if controller_kind in {ControllerKind.FLUX_KUSTOMIZATION, ControllerKind.FLUX_HELM_RELEASE}:
        source_evidence = {
            "source_ref": {
                "kind": "GitRepository",
                "name": "gitops",
                "namespace": controller_namespace,
            },
            **source_evidence,
        }
    return WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="apps",
        workload_kind="Deployment",
        workload_name="payments",
        provider_id="github-main",
        repo_url=repo_url,
        ref=ref,
        revision=None,
        path="apps/payments",
        controller_kind=controller_kind,
        controller_name=controller_name,
        controller_namespace=controller_namespace,
        source_revision=None,
        renderer=RendererKind.RAW,
        confidence=0.95,
        status=BindingStatus.RESOLVED,
        evidence=source_evidence,
    )


def _argo_application(revision: str = MERGE_SHA) -> dict[str, Any]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "payments", "namespace": "argocd"},
        "spec": {
            "source": {
                "repoURL": REPO_URL,
                "targetRevision": "main",
                "path": "apps/payments",
            }
        },
        "status": {
            "sync": {"status": "Synced", "revision": revision},
            "health": {"status": "Healthy"},
        },
    }


def _flux_controller(
    kind: str,
    revision: str,
    *,
    ready: str = "True",
    reason: str = "ReconciliationSucceeded",
) -> dict[str, Any]:
    api_version = "kustomize.toolkit.fluxcd.io/v1" if kind == "Kustomization" else "helm.toolkit.fluxcd.io/v2"
    revision_status = (
        {"lastAppliedRevision": revision} if kind == "Kustomization" else {"lastAttemptedRevision": revision}
    )
    source_ref = {"kind": "GitRepository", "name": "gitops"}
    spec = {"sourceRef": source_ref} if kind == "Kustomization" else {"chart": {"spec": {"sourceRef": source_ref}}}
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": "payments", "namespace": "flux-system", "generation": 4},
        "spec": spec,
        "status": {
            "observedGeneration": 4,
            **revision_status,
            "conditions": [
                {
                    "type": "Ready",
                    "status": ready,
                    "reason": reason,
                    "observedGeneration": 4,
                }
            ],
        },
    }


def _flux_git_repository(revision: str, *, ready: str = "True") -> dict[str, Any]:
    return {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": "gitops", "namespace": "flux-system", "generation": 2},
        "status": {
            "observedGeneration": 2,
            "artifact": {"revision": revision},
            "conditions": [
                {
                    "type": "Ready",
                    "status": ready,
                    "reason": "Succeeded" if ready == "True" else "Progressing",
                    "observedGeneration": 2,
                }
            ],
        },
    }


def _api(response: Mapping[str, Any]) -> _CustomObjectsApi:
    return _CustomObjectsApi(
        {
            "apiVersion": "v1",
            "kind": "List",
            "metadata": {},
            "items": [response],
        }
    )


@pytest.mark.asyncio
async def test_argo_single_source_reports_synced_for_exact_full_commit_sha():
    api = _api(_argo_application())

    result = await observe_gitops_sync(_source(), MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.SYNCED
    assert result.reason is GitOpsSyncReason.SYNC_CONFIRMED
    assert len(api.calls) == 1
    assert api.calls[0]["group"] == "argoproj.io"
    assert api.calls[0]["version"] == "v1alpha1"
    assert api.calls[0]["namespace"] == "argocd"
    assert api.calls[0]["plural"] == "applications"
    assert api.calls[0]["field_selector"] == "metadata.name=payments"
    assert api.calls[0]["limit"] == 2
    assert api.calls[0]["_request_timeout"] == 10


@pytest.mark.asyncio
async def test_manually_selected_argo_binding_keeps_syncable_controller_identity():
    candidate = _source()
    competing = candidate.model_copy(
        update={
            "id": "source-other",
            "repo_url": "https://github.com/acme/other.git",
        }
    )
    binding = WorkloadBindingResolver().resolve([candidate, competing], now=candidate.observed_at)
    selected = prepare_manual_binding_resolution(
        binding,
        candidate_id=candidate.id,
        generation_token=binding_candidate_generation_token(candidate),
        actor="operator",
        now=candidate.observed_at,
    ).binding.source

    assert selected is not None
    result = await observe_gitops_sync(selected, MERGE_SHA, _api(_argo_application()))

    assert selected.controller_kind is ControllerKind.ARGOCD
    assert selected.evidence["selected_via"] == "manual"
    assert result.status is GitOpsSyncStatus.SYNCED


@pytest.mark.asyncio
async def test_argo_single_source_accepts_full_sha256_commit():
    sha256_commit = "c" * 64
    result = await observe_gitops_sync(_source(), sha256_commit, _api(_argo_application(sha256_commit)))

    assert result.status is GitOpsSyncStatus.SYNCED


@pytest.mark.asyncio
async def test_argo_multi_source_uses_revision_at_persisted_source_index():
    source = _source(
        controller_namespace="argocd",
        repo_url="https://github.com/acme/overlays.git",
        evidence={"source_index": 1},
    )
    application = _argo_application()
    application["spec"].pop("source")
    application["spec"]["sources"] = [
        {"repoURL": REPO_URL, "targetRevision": "main", "path": "base"},
        {
            "repoURL": source.repo_url,
            "targetRevision": source.ref,
            "path": "apps/payments",
        },
    ]
    application["status"]["sync"] = {
        "status": "Synced",
        "revision": OTHER_SHA,
        "revisions": [OTHER_SHA, f"main@sha1:{MERGE_SHA}"],
    }

    result = await observe_gitops_sync(source, MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.SYNCED


@pytest.mark.asyncio
async def test_argo_multi_source_does_not_match_another_sources_revision():
    source = _source(
        repo_url="https://github.com/acme/overlays.git",
        evidence={"source_index": 1},
    )
    application = _argo_application()
    application["spec"].pop("source")
    application["spec"]["sources"] = [
        {"repoURL": REPO_URL, "targetRevision": "main", "path": "base"},
        {"repoURL": source.repo_url, "targetRevision": source.ref, "path": "apps/payments"},
    ]
    application["status"]["sync"] = {
        "status": "Synced",
        "revisions": [MERGE_SHA, OTHER_SHA],
    }

    result = await observe_gitops_sync(source, MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.MISMATCHED
    assert result.reason is GitOpsSyncReason.REVISION_MISMATCH


@pytest.mark.asyncio
async def test_argo_multi_source_without_index_rejects_ambiguous_repo_and_ref():
    source = _source(repo_url="https://github.com/acme/overlays.git")
    application = _argo_application()
    application["spec"].pop("source")
    application["spec"]["sources"] = [
        {"repoURL": source.repo_url, "targetRevision": source.ref, "path": "first"},
        {"repoURL": source.repo_url, "targetRevision": source.ref, "path": "second"},
    ]
    application["status"]["sync"] = {
        "status": "Synced",
        "revisions": [MERGE_SHA, MERGE_SHA],
    }

    result = await observe_gitops_sync(source, MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.MISMATCHED
    assert result.reason is GitOpsSyncReason.SOURCE_AMBIGUOUS


@pytest.mark.asyncio
async def test_argo_does_not_accept_commit_sha_as_an_arbitrary_substring():
    application = _argo_application(f"prefix-{MERGE_SHA}-suffix")

    result = await observe_gitops_sync(_source(), MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.UNAVAILABLE
    assert result.reason is GitOpsSyncReason.REVISION_UNAVAILABLE


@pytest.mark.asyncio
async def test_argo_reports_revision_mismatch_when_controller_is_healthy_and_synced():
    result = await observe_gitops_sync(_source(), MERGE_SHA, _api(_argo_application(OTHER_SHA)))

    assert result.status is GitOpsSyncStatus.MISMATCHED
    assert result.reason is GitOpsSyncReason.REVISION_MISMATCH


@pytest.mark.asyncio
async def test_argo_running_operation_is_pending_even_when_previous_sync_matches():
    application = _argo_application()
    application["status"]["operationState"] = {"phase": "Running"}

    result = await observe_gitops_sync(_source(), MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.PENDING
    assert result.reason is GitOpsSyncReason.RECONCILIATION_PENDING


@pytest.mark.asyncio
async def test_flux_kustomization_matches_explicit_artifact_revision_when_ready():
    source = _source(
        controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
        controller_namespace="flux-system",
    )
    controller = _flux_controller("Kustomization", f"main@sha1:{MERGE_SHA}")
    api = _api(controller)

    result = await observe_gitops_sync(source, MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.SYNCED
    assert api.calls[0]["group"] == "kustomize.toolkit.fluxcd.io"
    assert api.calls[0]["version"] == "v1"
    assert api.calls[0]["plural"] == "kustomizations"


@pytest.mark.asyncio
async def test_flux_helm_release_uses_git_repository_artifact_revision_and_readiness():
    source = _source(
        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
        controller_namespace="flux-system",
    )
    controller = _flux_controller("HelmRelease", "0.1.0")
    git_repository = _flux_git_repository(f"main@sha1:{MERGE_SHA}")
    api = _CustomObjectsApi(
        responses_by_plural={
            "helmreleases": {"items": [controller], "metadata": {}},
            "gitrepositories": {"items": [git_repository], "metadata": {}},
        }
    )

    result = await observe_gitops_sync(source, MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.SYNCED
    assert result.reason is GitOpsSyncReason.SYNC_CONFIRMED
    assert api.calls[0]["group"] == "helm.toolkit.fluxcd.io"
    assert api.calls[0]["version"] == "v2"
    assert api.calls[0]["plural"] == "helmreleases"
    assert api.calls[1]["group"] == "source.toolkit.fluxcd.io"
    assert api.calls[1]["version"] == "v1"
    assert api.calls[1]["namespace"] == "flux-system"
    assert api.calls[1]["plural"] == "gitrepositories"
    assert api.calls[1]["field_selector"] == "metadata.name=gitops"


@pytest.mark.asyncio
async def test_manually_selected_flux_helmrelease_binding_keeps_source_identity_for_sync():
    candidate = _source(
        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
        controller_namespace="flux-system",
    )
    competing = candidate.model_copy(
        update={
            "id": "source-flux-other",
            "repo_url": "https://github.com/acme/other.git",
        }
    )
    binding = WorkloadBindingResolver().resolve([candidate, competing], now=candidate.observed_at)
    selected = prepare_manual_binding_resolution(
        binding,
        candidate_id=candidate.id,
        generation_token=binding_candidate_generation_token(candidate),
        actor="operator",
        now=candidate.observed_at,
    ).binding.source
    api = _CustomObjectsApi(
        responses_by_plural={
            "helmreleases": {"items": [_flux_controller("HelmRelease", "0.1.0")], "metadata": {}},
            "gitrepositories": {
                "items": [_flux_git_repository(f"main@sha1:{MERGE_SHA}")],
                "metadata": {},
            },
        }
    )

    assert selected is not None
    result = await observe_gitops_sync(selected, MERGE_SHA, api)

    assert selected.controller_kind is ControllerKind.FLUX_HELM_RELEASE
    assert selected.evidence["source_ref"]["name"] == "gitops"
    assert selected.evidence["selected_via"] == "manual"
    assert result.status is GitOpsSyncStatus.SYNCED


@pytest.mark.asyncio
async def test_flux_helm_release_waits_for_git_repository_merge_revision():
    source = _source(
        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
        controller_namespace="flux-system",
    )
    api = _CustomObjectsApi(
        responses_by_plural={
            "helmreleases": {"items": [_flux_controller("HelmRelease", "0.1.0")], "metadata": {}},
            "gitrepositories": {"items": [_flux_git_repository(f"main@sha1:{OTHER_SHA}")], "metadata": {}},
        }
    )

    result = await observe_gitops_sync(source, MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.MISMATCHED
    assert result.reason is GitOpsSyncReason.REVISION_MISMATCH


@pytest.mark.asyncio
async def test_flux_helm_release_with_chart_version_only_is_unavailable():
    source = _source(
        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
        controller_namespace="flux-system",
    )
    api = _CustomObjectsApi(
        responses_by_plural={
            "helmreleases": {"items": [_flux_controller("HelmRelease", "0.1.0")], "metadata": {}},
            "gitrepositories": {"items": [_flux_git_repository("0.1.0")], "metadata": {}},
        }
    )

    result = await observe_gitops_sync(source, MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.UNAVAILABLE
    assert result.reason is GitOpsSyncReason.REVISION_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("controller_kind", "resource_kind", "source_ref_path"),
    [
        (ControllerKind.FLUX_KUSTOMIZATION, "Kustomization", ("sourceRef",)),
        (ControllerKind.FLUX_HELM_RELEASE, "HelmRelease", ("chart", "spec", "sourceRef")),
    ],
)
async def test_flux_source_reference_must_match_persisted_source_identity(
    controller_kind: ControllerKind,
    resource_kind: str,
    source_ref_path: tuple[str, ...],
):
    source = _source(
        controller_kind=controller_kind,
        controller_namespace="flux-system",
    )
    controller = _flux_controller(resource_kind, f"main@sha1:{MERGE_SHA}")
    configured_ref = controller["spec"]
    for path_part in source_ref_path:
        configured_ref = configured_ref[path_part]
    configured_ref["name"] = "different-git-repository"

    result = await observe_gitops_sync(source, MERGE_SHA, _api(controller))

    assert result.status is GitOpsSyncStatus.MISMATCHED
    assert result.reason is GitOpsSyncReason.SOURCE_MISMATCH


@pytest.mark.asyncio
async def test_flux_not_ready_is_pending_even_when_revision_matches():
    source = _source(
        controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
        controller_namespace="flux-system",
    )
    controller = _flux_controller("Kustomization", MERGE_SHA, ready="False", reason="Progressing")

    result = await observe_gitops_sync(source, MERGE_SHA, _api(controller))

    assert result.status is GitOpsSyncStatus.PENDING
    assert result.reason is GitOpsSyncReason.RECONCILIATION_PENDING


@pytest.mark.asyncio
async def test_flux_stale_ready_condition_is_pending():
    source = _source(
        controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
        controller_namespace="flux-system",
    )
    controller = _flux_controller("Kustomization", MERGE_SHA)
    controller["metadata"]["generation"] = 5

    result = await observe_gitops_sync(source, MERGE_SHA, _api(controller))

    assert result.status is GitOpsSyncStatus.PENDING
    assert result.reason is GitOpsSyncReason.RECONCILIATION_PENDING


@pytest.mark.asyncio
async def test_flux_failed_condition_is_reported_without_exposing_condition_payload():
    source = _source(
        controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
        controller_namespace="flux-system",
    )
    controller = _flux_controller(
        "Kustomization",
        MERGE_SHA,
        ready="False",
        reason="ReconciliationFailed",
    )
    controller["status"]["conditions"][0]["message"] = "private controller details"

    result = await observe_gitops_sync(source, MERGE_SHA, _api(controller))

    assert result.status is GitOpsSyncStatus.FAILED
    assert result.reason is GitOpsSyncReason.CONTROLLER_FAILED
    assert "private controller details" not in repr(result)


@pytest.mark.asyncio
async def test_ambiguous_exact_controller_matches_fail_closed():
    application = _argo_application()
    api = _CustomObjectsApi({"items": [application, deepcopy(application)], "metadata": {}})

    result = await observe_gitops_sync(_source(), MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.MISMATCHED
    assert result.reason is GitOpsSyncReason.CONTROLLER_AMBIGUOUS


@pytest.mark.asyncio
async def test_missing_controller_is_unavailable():
    result = await observe_gitops_sync(_source(), MERGE_SHA, _CustomObjectsApi())

    assert result.status is GitOpsSyncStatus.UNAVAILABLE
    assert result.reason is GitOpsSyncReason.CONTROLLER_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_sha", ["a" * 39, "a" * 41, "g" * 40, f" {MERGE_SHA}", ""])
async def test_malformed_merge_sha_is_rejected_before_api_read(merge_sha: str):
    api = _CustomObjectsApi()

    with pytest.raises(ValueError, match="full commit SHA"):
        await observe_gitops_sync(_source(), merge_sha, api)

    assert api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
async def test_api_permission_and_transient_errors_are_rethrown(status_code: int):
    error = ApiException(status=status_code, reason="private API error")
    api = _CustomObjectsApi(error=error)

    with pytest.raises(ApiException) as raised:
        await observe_gitops_sync(_source(), MERGE_SHA, api)

    assert raised.value is error


@pytest.mark.asyncio
async def test_api_timeout_is_rethrown_for_retry():
    error = TimeoutError("private timeout details")
    api = _CustomObjectsApi(error=error)

    with pytest.raises(TimeoutError) as raised:
        await observe_gitops_sync(_source(), MERGE_SHA, api)

    assert raised.value is error


@pytest.mark.asyncio
async def test_missing_resource_api_404_is_unavailable():
    api = _CustomObjectsApi(error=ApiException(status=404, reason="Not found"))

    result = await observe_gitops_sync(_source(), MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.UNAVAILABLE
    assert result.reason is GitOpsSyncReason.CONTROLLER_NOT_FOUND


@pytest.mark.asyncio
async def test_truncated_controller_list_fails_closed():
    application = _argo_application()
    api = _CustomObjectsApi({"items": [application], "metadata": {"_continue": "next"}})

    result = await observe_gitops_sync(_source(), MERGE_SHA, api)

    assert result.status is GitOpsSyncStatus.UNAVAILABLE
    assert result.reason is GitOpsSyncReason.RESPONSE_TRUNCATED


@pytest.mark.asyncio
async def test_controller_with_wrong_identity_fails_closed():
    application = _argo_application()
    application["metadata"]["namespace"] = "other-namespace"

    result = await observe_gitops_sync(_source(), MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.MISMATCHED
    assert result.reason is GitOpsSyncReason.CONTROLLER_IDENTITY_MISMATCH


@pytest.mark.asyncio
async def test_controller_with_unexpected_api_version_is_unavailable():
    application = _argo_application()
    application["apiVersion"] = "v1"

    result = await observe_gitops_sync(_source(), MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.UNAVAILABLE
    assert result.reason is GitOpsSyncReason.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_invalid_controller_name_is_rejected_before_api_read():
    api = _CustomObjectsApi()

    with pytest.raises(ValueError, match="controller name"):
        await observe_gitops_sync(_source(controller_name="bad/name"), MERGE_SHA, api)

    assert api.calls == []


@pytest.mark.asyncio
async def test_oversized_controller_response_fails_closed():
    application = _argo_application()
    application["status"]["unused"] = "x" * 600_000

    result = await observe_gitops_sync(_source(), MERGE_SHA, _api(application))

    assert result.status is GitOpsSyncStatus.UNAVAILABLE
    assert result.reason is GitOpsSyncReason.RESPONSE_TOO_LARGE
