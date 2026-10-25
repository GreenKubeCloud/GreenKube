import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from greenkube.gitops.contracts import BindingStatus, ControllerKind, RendererKind
from greenkube.gitops.discovery.argo import discover_argo_application
from greenkube.gitops.discovery.flux import (
    discover_flux_helm_release,
    discover_flux_helm_release_values_sources,
    discover_flux_kustomization,
    parse_flux_inventory_id,
)

FIXTURES = Path(__file__).parent / "fixtures"
_YAML = YAML(typ="safe")


def _fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as stream:
        value = _YAML.load(stream)
    assert isinstance(value, dict)
    return value


def _application(resource: Mapping[str, Any], *, status: dict[str, Any] | None = None) -> dict[str, Any]:
    application = {
        "metadata": {"name": "payments-prod", "namespace": "argocd"},
        "spec": {
            "destination": {"namespace": "prod"},
            "source": {
                "repoURL": "https://github.com/acme/apps.git",
                "targetRevision": "main",
                "path": "apps/payments",
            },
        },
        "status": {
            "sync": {"revision": "a" * 40},
            "resources": [dict(resource)],
        },
    }
    if status is not None:
        application["status"] = status
    return application


def _argo(application: Mapping[str, Any], **overrides: Any):
    arguments = {"cluster": "prod", "provider_id": "github-main"}
    arguments.update(overrides)
    return discover_argo_application(application, **arguments)


def _flux_resource(name: str = "payments-api") -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": "prod",
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
            "annotations": {
                "meta.helm.sh/release-name": "payments",
                "meta.helm.sh/release-namespace": "prod",
            },
        },
    }


def test_argo_single_source_maps_live_resources_and_deployed_revision():
    application = _fixture("argo-raw.yaml")

    sources = _argo(application)

    assert len(sources) == 1
    source = sources[0]
    assert source.status is BindingStatus.RESOLVED
    assert source.controller_kind is ControllerKind.ARGOCD
    assert source.controller_name == "payments-prod"
    assert source.controller_namespace == "argocd"
    assert (source.workload_kind, source.workload_name, source.namespace) == ("Deployment", "payments-api", "prod")
    assert source.repo_url == "https://github.com/acme/gitops.git"
    assert source.ref == "main"
    assert source.path == "apps/payments"
    assert source.renderer is RendererKind.RAW
    assert source.source_revision == "a" * 40
    assert source.revision == "a" * 40
    assert source.evidence["resource"]["group"] == "apps"


def test_argo_source_less_application_returns_a_blocked_candidate_without_indexing_negative_revisions():
    application = {
        "metadata": {"name": "source-less", "namespace": "argocd"},
        "status": {
            "resources": [
                {"kind": "Deployment", "namespace": "prod", "name": "payments-api"},
            ]
        },
    }

    sources = _argo(application)

    assert len(sources) == 1
    assert sources[0].status is BindingStatus.BLOCKED
    assert sources[0].reason == "workload-source-unresolved"
    assert sources[0].source_revision is None


def test_argo_candidate_carries_exact_controller_identity_and_generation():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["metadata"].update({"uid": "application-uid", "generation": 9})

    source = _argo(application)[0]

    assert source.controller_uid == "application-uid"
    assert source.controller_generation == 9


def test_argo_classifies_kustomize_and_preserves_overlay_metadata():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["kustomize"] = {
        "patches": [{"patch": "spec: {}", "target": {"kind": "Deployment"}}],
        "components": ["../components/metrics"],
        "images": [{"name": "api", "newTag": "v2"}],
        "helmCharts": [{"name": "sidecar", "version": "1.0.0"}],
    }

    source = _argo(application)[0]

    assert source.renderer is RendererKind.KUSTOMIZE
    assert source.renderer_config["overlay_path"] == "apps/payments"
    assert source.renderer_config["kustomization_path"] == "apps/payments/kustomization.yaml"
    assert source.renderer_config["patches"] == application["spec"]["source"]["kustomize"]["patches"]
    assert source.renderer_config["components"] == ["../components/metrics"]
    assert source.renderer_config["images"] == [{"name": "api", "newTag": "v2"}]
    assert source.renderer_config["helm_charts"] == [{"name": "sidecar", "version": "1.0.0"}]


def test_argo_uses_caller_supplied_kustomization_path_without_repository_access():
    application = _fixture("argo-raw.yaml")

    source = _argo(application, kustomization_paths=["apps/payments"])[0]

    assert source.renderer is RendererKind.KUSTOMIZE
    assert source.renderer_config["kustomization_path"] == "apps/payments/kustomization.yaml"


def test_argo_uses_live_source_type_for_auto_detected_kustomize():
    application = _fixture("argo-raw.yaml")
    application["status"]["sourceType"] = "Kustomize"

    source = _argo(application)[0]

    assert source.renderer is RendererKind.KUSTOMIZE
    assert source.renderer_config["overlay_path"] == "apps/payments"


def test_argo_does_not_apply_source_type_to_multi_source_applications():
    application = _fixture("argo-multi-source.yaml")
    application["status"]["sourceType"] = "Kustomize"

    source = _argo(application)[0]

    assert source.renderer is RendererKind.HELM_VALUES


def test_argo_multi_source_resolves_ordered_local_and_ref_values_files():
    application = _fixture("argo-multi-source.yaml")

    sources = _argo(application)

    assert len(sources) == 1
    source = sources[0]
    assert source.status is BindingStatus.RESOLVED
    assert source.renderer is RendererKind.HELM_VALUES
    assert source.repo_url == "https://github.com/acme/apps.git"
    assert source.source_revision == "b" * 40
    assert source.renderer_config["values_files"] == [
        "apps/payments/values-common.yaml",
        "environments/prod/payments.yaml",
    ]
    values_sources = source.renderer_config["values_sources"]
    assert [entry["source_ref"] for entry in values_sources] == [None, "values"]
    assert [entry["repo_url"] for entry in values_sources] == [
        "https://github.com/acme/apps.git",
        "https://github.com/acme/env-config.git",
    ]
    assert [entry["path"] for entry in values_sources] == [
        "apps/payments/values-common.yaml",
        "environments/prod/payments.yaml",
    ]
    assert [entry["source_revision"] for entry in values_sources] == [
        "b" * 40,
        "c" * 40,
    ]
    assert source.renderer_config["release_name"] == "payments"
    assert source.renderer_config["release_namespace"] == "prod"


def test_argo_remote_helm_chart_binds_to_git_managed_values_source():
    application = _fixture("argo-multi-source.yaml")
    application["metadata"]["annotations"] = {
        "greenkube.cloud/helm-values-mapping": json.dumps(
            {
                "file_path": "environments/prod/payments.yaml",
                "container": "api",
                "path": ["controller", "container", "resources"],
                "source_index": 1,
            }
        )
    }
    application["spec"]["sources"][0] = {
        "repoURL": "https://charts.fairwinds.com/stable",
        "chart": "vpa",
        "targetRevision": "5.1.0",
        "helm": {
            "releaseName": "vpa",
            "namespace": "prod",
            "valueFiles": ["$values/environments/prod/payments.yaml"],
        },
    }
    application["spec"]["sources"][1] = {
        "repoURL": "https://github.com/acme/env-config.git",
        "targetRevision": "release",
        "ref": "values",
    }
    application["status"]["sync"]["revisions"] = ["5.1.0", "c" * 40]

    source = _argo(application)[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.repo_url == "https://github.com/acme/env-config.git"
    assert source.ref == "release"
    assert source.revision == "c" * 40
    assert source.renderer_config["chart"]["repo"] == "https://charts.fairwinds.com/stable"
    assert source.renderer_config["values_mapping"]["source_index"] == 1


def test_argo_local_helm_chart_without_value_files_keeps_chart_defaults_actionable():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["helm"] = {}

    source = _argo(application)[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer is RendererKind.HELM_VALUES
    assert source.renderer_config["values_files"] == []


def test_argo_persists_an_explicit_helm_values_mapping_annotation():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["helm"] = {"valueFiles": ["values.yaml"]}
    application["metadata"]["annotations"] = {
        "greenkube.cloud/helm-values-mapping": json.dumps(
            {
                "file_path": "apps/payments/values.yaml",
                "container": "api",
                "path": ["controller", "container", "resources"],
            }
        )
    }

    source = _argo(application)[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer is RendererKind.HELM_VALUES
    assert source.renderer_config["values_mapping"] == {
        "file_path": "apps/payments/values.yaml",
        "container": "api",
        "path": ["controller", "container", "resources"],
    }


def test_argo_infers_a_unique_source_index_for_an_explicit_multi_source_mapping():
    application = _fixture("argo-multi-source.yaml")
    application["metadata"]["annotations"] = {
        "greenkube.cloud/helm-values-mapping": json.dumps(
            {
                "file_path": "apps/payments/values-common.yaml",
                "container": "api",
                "path": ["controller", "container", "resources"],
            }
        )
    }

    source = _argo(application)[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer_config["values_mapping"] == {
        "file_path": "apps/payments/values-common.yaml",
        "container": "api",
        "path": ["controller", "container", "resources"],
        "source_index": 0,
    }


def test_argo_mapping_source_index_can_select_a_values_source_distinct_from_the_chart_owner():
    application = _fixture("argo-multi-source.yaml")
    application["metadata"]["annotations"] = {
        "greenkube.cloud/helm-values-mapping": json.dumps(
            {
                "file_path": "environments/prod/payments.yaml",
                "container": "api",
                "path": ["controller", "container", "resources"],
                "source_index": 1,
            }
        )
    }

    source = _argo(application)[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer_config["values_mapping"]["source_index"] == 1
    assert source.renderer_config["values_mapping"]["file_path"] == "environments/prod/payments.yaml"


def test_argo_rejects_malformed_helm_values_mapping_annotation():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["helm"] = {"valueFiles": ["values.yaml"]}
    application["metadata"]["annotations"] = {"greenkube.cloud/helm-values-mapping": "{not-json"}

    source = _argo(application)[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "invalid-helm-values-mapping"


def test_argo_operation_revision_is_used_when_sync_revision_is_missing():
    application = _application(
        {"kind": "Deployment", "namespace": "prod", "name": "payments-api"},
        status={"operationState": {"syncResult": {"revision": "f" * 40}}, "resources": []},
    )
    application["status"]["resources"] = [{"kind": "Deployment", "namespace": "prod", "name": "payments-api"}]

    source = _argo(application)[0]

    assert source.source_revision == "f" * 40
    assert source.revision == "f" * 40


def test_argo_uses_tracking_id_only_when_status_resources_are_empty():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["metadata"]["annotations"] = {
        "argocd.argoproj.io/tracking-id": "payments-prod:apps/Deployment:prod/payments-api"
    }
    application["status"]["resources"] = []

    source = _argo(application)[0]

    assert source.workload_kind == "Deployment"
    assert source.workload_name == "payments-api"
    assert source.evidence["tracking_id_fallback"] is True


def test_argo_does_not_use_tracking_id_owned_by_another_application():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["metadata"]["annotations"] = {
        "argocd.argoproj.io/tracking-id": "another-app:apps/Deployment:prod/payments-api"
    }
    application["status"]["resources"] = []

    assert _argo(application) == []


def test_argo_blocks_inline_helm_values_without_git_owned_application():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["helm"] = {"valuesObject": {"resources": {"requests": {"cpu": "500m"}}}}

    source = _argo(application)[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "application-not-in-git"


def test_argo_resolves_inline_helm_values_only_with_explicit_application_source():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["helm"] = {"values": "resources:\n  requests:\n    cpu: 500m\n"}
    application_source = {
        "provider_id": "github-config",
        "repo_url": "https://github.com/acme/config.git",
        "ref": "main",
        "revision": "e" * 40,
        "path": "clusters/prod/argocd-apps/payments.yaml",
    }

    source = _argo(application, application_source=application_source)[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.provider_id == "github-config"
    assert source.repo_url == application_source["repo_url"]
    assert source.path == application_source["path"]
    assert source.revision == "e" * 40


def test_argo_ambiguous_multi_source_ownership_is_not_guessed():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    base = application["spec"]["source"]
    application["spec"].pop("source")
    application["spec"]["sources"] = [
        base,
        {"repoURL": "https://github.com/acme/other.git", "targetRevision": "main", "path": "other"},
    ]

    sources = _argo(application)

    assert len(sources) == 2
    assert all(source.status is BindingStatus.AMBIGUOUS for source in sources)
    assert all(source.reason == "ambiguous-multi-source-owner" for source in sources)


def test_argo_blocks_unsafe_paths_in_value_files():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["helm"] = {"valueFiles": ["../../secrets.yaml"]}

    source = _argo(application)[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "unsafe-values-file-path"


def test_argo_blocks_unsafe_application_source_paths():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["path"] = "../outside"

    source = _argo(application)[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "unsafe-source-path"
    assert source.path == "."


def test_argo_blocks_unsupported_config_management_plugins():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["plugin"] = {"name": "custom-generator"}

    source = _argo(application)[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "unsupported-config-management-plugin"


def test_argo_blocks_unsupported_jsonnet_directory_rendering():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["directory"] = {"jsonnet": {"extVars": [{"name": "cluster", "value": "prod"}]}}

    source = _argo(application)[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "unsupported-directory-renderer"


def test_argo_accepts_empty_jsonnet_defaults_on_directory_source():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"]["source"]["directory"] = {"jsonnet": {}, "recurse": True}

    source = _argo(application)[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.reason is None
    assert source.renderer is RendererKind.RAW


def test_argo_blocks_unresolved_values_source_references():
    application = _application({"kind": "Deployment", "namespace": "prod", "name": "payments-api"})
    application["spec"].pop("source")
    application["spec"]["sources"] = [
        {
            "repoURL": "https://github.com/acme/apps.git",
            "targetRevision": "main",
            "path": "apps/payments",
            "helm": {"valueFiles": ["$missing/values.yaml"]},
        }
    ]

    source = _argo(application)[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "unresolved-values-reference"


def test_parse_flux_inventory_id_supports_core_and_grouped_resources():
    grouped = parse_flux_inventory_id("apps_v1_Deployment_prod_payments-api")
    core = parse_flux_inventory_id("_v1_Pod_prod_api-123")

    assert (grouped.api_version, grouped.kind, grouped.namespace, grouped.name) == (
        "apps/v1",
        "Deployment",
        "prod",
        "payments-api",
    )
    assert (core.api_version, core.kind, core.namespace, core.name) == ("v1", "Pod", "prod", "api-123")


def test_parse_flux_inventory_id_supports_current_namespace_name_group_kind_format():
    target = parse_flux_inventory_id(
        "greenkube-qa-flux-kustomize_flux-kustomize_apps_Deployment",
        version="v1",
    )

    assert (target.api_version, target.kind, target.namespace, target.name) == (
        "apps/v1",
        "Deployment",
        "greenkube-qa-flux-kustomize",
        "flux-kustomize",
    )


@pytest.mark.parametrize(
    "inventory_id",
    [
        "",
        "apps_v1_Deployment_prod",
        "apps_v1_Deployment_prod_bad_name",
        "apps_v1_Deployment_../payments-api",
        "apps_v1_Deployment_prod_",
    ],
)
def test_parse_flux_inventory_id_rejects_malformed_or_unsafe_ids(inventory_id: str):
    with pytest.raises(ValueError):
        parse_flux_inventory_id(inventory_id)


def test_flux_kustomization_maps_inventory_source_and_artifact_revision():
    kustomization = _fixture("flux-kustomization.yaml")
    git_repository = _fixture("flux-gitrepository.yaml")

    sources = discover_flux_kustomization(
        kustomization,
        [git_repository],
        cluster="prod",
        provider_id="github-main",
    )

    assert len(sources) == 1
    source = sources[0]
    assert source.status is BindingStatus.RESOLVED
    assert source.controller_kind is ControllerKind.FLUX_KUSTOMIZATION
    assert source.path == "apps/payments/overlays/prod"
    assert source.renderer is RendererKind.KUSTOMIZE
    assert source.source_revision == "main@sha1:" + "d" * 40
    assert source.revision == "d" * 40
    assert source.evidence["inventory_id"] == "apps_v1_Deployment_prod_payments-api"


def test_flux_candidate_carries_controller_and_source_generations():
    kustomization = _fixture("flux-kustomization.yaml")
    kustomization["metadata"].update({"uid": "kustomization-uid", "generation": 11})
    git_repository = _fixture("flux-gitrepository.yaml")
    git_repository["metadata"].update({"uid": "git-source-uid", "generation": 5})

    source = discover_flux_kustomization(
        kustomization,
        [git_repository],
        cluster="prod",
        provider_id="github-main",
    )[0]

    assert source.controller_uid == "kustomization-uid"
    assert source.controller_generation == 11
    assert source.source_uid == "git-source-uid"
    assert source.source_generation == 5


def test_flux_kustomization_classifies_caller_supplied_plain_manifest_path_as_raw():
    kustomization = _fixture("flux-kustomization.yaml")

    source = discover_flux_kustomization(
        kustomization,
        [_fixture("flux-gitrepository.yaml")],
        cluster="prod",
        provider_id="github-main",
        plain_manifest_paths=["apps/payments/overlays/prod"],
    )[0]

    assert source.renderer is RendererKind.RAW
    assert source.renderer_config["manifest_path"] == "apps/payments/overlays/prod"


def test_flux_kustomization_skips_malformed_inventory_entries():
    kustomization = _fixture("flux-kustomization.yaml")
    kustomization["status"]["inventory"]["entries"].append({"id": "bad"})
    kustomization["status"]["inventory"]["entries"].append(dict(kustomization["status"]["inventory"]["entries"][0]))

    sources = discover_flux_kustomization(
        kustomization,
        [_fixture("flux-gitrepository.yaml")],
        cluster="prod",
        provider_id="github-main",
    )

    assert len(sources) == 1
    assert sources[0].workload_name == "payments-api"


def test_flux_kustomization_reports_unresolved_source_ref():
    kustomization = _fixture("flux-kustomization.yaml")

    sources = discover_flux_kustomization(kustomization, [], cluster="prod", provider_id="github-main")

    assert len(sources) == 1
    assert sources[0].status is BindingStatus.UNRESOLVED
    assert sources[0].reason == "source-ref-unresolved"


def test_flux_kustomization_fails_closed_for_duplicate_source_refs():
    kustomization = _fixture("flux-kustomization.yaml")
    git_repository = _fixture("flux-gitrepository.yaml")
    second_repository = _fixture("flux-gitrepository.yaml")
    second_repository["spec"]["url"] = "https://github.com/acme/other.git"

    sources = discover_flux_kustomization(
        kustomization,
        [git_repository, second_repository],
        cluster="prod",
        provider_id="github-main",
    )

    assert len(sources) == 2
    assert all(source.status is BindingStatus.AMBIGUOUS for source in sources)
    assert all(source.reason == "source-ref-ambiguous" for source in sources)


def test_flux_kustomization_resolves_bucket_source_url():
    kustomization = _fixture("flux-kustomization.yaml")
    kustomization["spec"]["sourceRef"] = {"kind": "Bucket", "name": "manifests"}
    bucket = {
        "kind": "Bucket",
        "metadata": {"name": "manifests", "namespace": "flux-system"},
        "spec": {"endpoint": "minio.example.test:9000", "bucketName": "cluster-state", "insecure": True},
        "status": {"artifact": {"revision": "bucket-revision"}},
    }

    source = discover_flux_kustomization(
        kustomization,
        [bucket],
        cluster="prod",
        provider_id="object-store",
    )[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.repo_url == "http://minio.example.test:9000/cluster-state"
    assert source.source_revision == "bucket-revision"
    assert source.revision is None


def test_flux_kustomization_resolves_oci_source_without_treating_digest_as_git_commit():
    kustomization = _fixture("flux-kustomization.yaml")
    kustomization["spec"]["sourceRef"] = {"kind": "OCIRepository", "name": "manifests"}
    oci_repository = {
        "kind": "OCIRepository",
        "metadata": {"name": "manifests", "namespace": "flux-system"},
        "spec": {"url": "oci://registry.example.test/cluster-state", "ref": {"tag": "v1.2.3"}},
        "status": {"artifact": {"revision": "v1.2.3@sha256:" + "e" * 64}},
    }

    source = discover_flux_kustomization(
        kustomization,
        [oci_repository],
        cluster="prod",
        provider_id="registry-main",
    )[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.repo_url == "oci://registry.example.test/cluster-state"
    assert source.ref == "v1.2.3"
    assert source.source_revision == "v1.2.3@sha256:" + "e" * 64
    assert source.revision is None


def test_flux_helm_release_matches_exact_release_identity_and_maps_renderer_metadata():
    release = _fixture("flux-helmrelease.yaml")
    helm_repository = _fixture("flux-helmrepository.yaml")
    manifest_source = {
        "provider_id": "github-config",
        "repo_url": "https://github.com/acme/platform.git",
        "ref": "main",
        "revision": "a" * 40,
        "path": "clusters/prod/releases/payments.yaml",
    }
    git_managed_values = {
        ("ConfigMap", "prod", "payments-values"): {
            "repo_url": "https://github.com/acme/platform.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod/values/payments.yaml",
        },
        ("Secret", "prod", "payments-secrets"): {
            "repo_url": "https://github.com/acme/secrets.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod/values/payments-secrets.yaml",
            "data": {"password": "never-disclose-secret-value"},
        },
    }

    sources = discover_flux_helm_release(
        release,
        [_flux_resource()],
        [helm_repository],
        cluster="prod",
        provider_id="github-main",
        manifest_source=manifest_source,
        git_managed_values=git_managed_values,
    )

    assert len(sources) == 1
    source = sources[0]
    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "values-source-not-in-snapshot"
    assert source.renderer is RendererKind.HELM_RELEASE
    assert source.workload_name == "payments-api"
    assert source.repo_url == manifest_source["repo_url"]
    assert source.provider_id == "github-config"
    assert source.path == manifest_source["path"]
    assert source.source_revision == "sha256:" + "e" * 64
    assert source.revision == "a" * 40
    assert source.renderer_config["chart"] == {
        "name": "payments",
        "version": "1.4.2",
        "repo_url": "https://charts.example.test",
        "source_kind": "HelmRepository",
    }
    assert source.renderer_config["release_name"] == "payments"
    assert source.renderer_config["release_namespace"] == "prod"
    assert source.renderer_config["values"]["resources"]["requests"]["cpu"] == "500m"
    assert [value["kind"] for value in source.renderer_config["values_from"]] == ["ConfigMap", "Secret"]
    assert [value["path"] for value in source.renderer_config["values_from"]] == [
        "clusters/prod/values/payments.yaml",
        "clusters/prod/values/payments-secrets.yaml",
    ]
    assert "data" not in source.renderer_config["values_from"][1]
    assert "never-disclose-secret-value" not in json.dumps(source.model_dump(mode="json"))
    assert source.renderer_config["post_renderers"] == release["spec"]["postRenderers"]

    mismatched_release = _flux_resource(name="another-api")
    mismatched_release["metadata"]["annotations"]["meta.helm.sh/release-name"] = "another-release"
    assert (
        discover_flux_helm_release(
            release,
            [mismatched_release],
            [helm_repository],
            cluster="prod",
            provider_id="github-main",
            manifest_source=manifest_source,
            git_managed_values=git_managed_values,
        )
        == []
    )


def test_flux_helm_release_resolves_default_release_name_from_target_namespace():
    release = _fixture("flux-helmrelease.yaml")
    release["spec"].pop("releaseName")
    release["spec"]["valuesFrom"] = []
    release["spec"]["chart"]["spec"]["sourceRef"] = {
        "kind": "GitRepository",
        "name": "gitops",
        "namespace": "flux-system",
    }
    resource = _flux_resource()
    resource["metadata"]["annotations"]["meta.helm.sh/release-name"] = "prod-payments"

    sources = discover_flux_helm_release(
        release,
        [resource],
        [_fixture("flux-gitrepository-gitops.yaml")],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/gitops.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod/releases/payments.yaml",
        },
    )

    assert len(sources) == 1
    assert sources[0].status is BindingStatus.RESOLVED
    assert sources[0].renderer_config["release_name"] == "prod-payments"
    assert sources[0].evidence["release_identity"]["name"] == "prod-payments"


def test_flux_helm_release_defaults_release_namespace_and_name_to_the_helmrelease():
    release = _fixture("flux-helmrelease-default-name.yaml")
    release["spec"].pop("targetNamespace")
    resource = _flux_resource()
    resource["metadata"]["namespace"] = "flux-system"
    resource["metadata"]["annotations"].update(
        {
            "meta.helm.sh/release-name": "payments",
            "meta.helm.sh/release-namespace": "flux-system",
        }
    )

    source = discover_flux_helm_release(
        release,
        [resource],
        [_fixture("flux-gitrepository-gitops.yaml")],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/gitops.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod/releases/payments.yaml",
        },
    )[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer_config["release_name"] == "payments"
    assert source.renderer_config["release_namespace"] == "flux-system"
    assert source.evidence["release_identity"] == {"name": "payments", "namespace": "flux-system"}


def test_flux_helm_release_does_not_hash_or_truncate_flux_default_release_names():
    release = _fixture("flux-helmrelease.yaml")
    controller_name = f"payments-{'x' * 55}"
    release["metadata"]["name"] = controller_name
    release["spec"].pop("releaseName")
    release["spec"]["valuesFrom"] = []
    expected_name = f"prod-{controller_name}"
    resource = _flux_resource()
    resource["metadata"]["annotations"]["meta.helm.sh/release-name"] = expected_name

    sources = discover_flux_helm_release(
        release,
        [resource],
        [_fixture("flux-helmrepository.yaml")],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/platform.git",
            "ref": "main",
            "path": "clusters/prod/releases/payments.yaml",
        },
    )

    assert len(sources) == 1
    assert sources[0].renderer_config["release_name"] == expected_name
    assert len(expected_name) > 53


def test_flux_helm_release_blocks_remote_chart_ref_with_a_precise_reason():
    release = _fixture("flux-helmrelease.yaml")
    release["spec"].pop("chart")
    release["spec"]["chartRef"] = {
        "kind": "OCIRepository",
        "name": "payments-chart",
        "namespace": "flux-system",
    }
    release["spec"]["valuesFrom"] = []
    oci_repository = {
        "kind": "OCIRepository",
        "metadata": {"name": "payments-chart", "namespace": "flux-system"},
        "spec": {"url": "oci://registry.example.test/payments"},
    }

    source = discover_flux_helm_release(
        release,
        [_flux_resource()],
        [oci_repository],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/platform.git",
            "ref": "main",
            "path": "clusters/prod/releases/payments.yaml",
        },
    )[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "remote-chart-not-in-snapshot"
    assert source.evidence["chart_ref"] == {
        "kind": "OCIRepository",
        "name": "payments-chart",
        "namespace": "flux-system",
    }


def test_flux_helm_release_discovers_chartref_helmchart_from_the_matching_git_source():
    release = _fixture("flux-helmrelease-chartref.yaml")
    helm_chart = _fixture("flux-helmchart.yaml")
    git_repository = _fixture("flux-gitrepository-gitops.yaml")
    resource = _flux_resource()
    resource["metadata"]["annotations"]["meta.helm.sh/release-name"] = "prod-payments"

    source = discover_flux_helm_release(
        release,
        [resource],
        [helm_chart, git_repository],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "provider_id": "github-main",
            "repo_url": "https://github.com/acme/gitops.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod/releases/payments.yaml",
        },
    )[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer_config["chart"] == {
        "name": "./charts/payments",
        "version": "1.0.0",
        "repo_url": "https://github.com/acme/gitops.git",
        "source_kind": "GitRepository",
    }
    assert source.renderer_config["chart_values_files"] == ["charts/payments/values-prod.yaml"]
    assert source.evidence["chart_ref"] == {
        "kind": "HelmChart",
        "name": "payments-chart",
        "namespace": "flux-system",
        "uid": "helmchart-uid",
        "generation": 3,
    }


def test_flux_helm_release_blocks_chartref_from_a_different_git_repository_snapshot():
    release = _fixture("flux-helmrelease-chartref.yaml")
    helm_chart = _fixture("flux-helmchart.yaml")
    git_repository = _fixture("flux-gitrepository-gitops.yaml")
    git_repository["spec"]["url"] = "https://github.com/acme/charts.git"
    resource = _flux_resource()
    resource["metadata"]["annotations"]["meta.helm.sh/release-name"] = "prod-payments"

    source = discover_flux_helm_release(
        release,
        [resource],
        [helm_chart, git_repository],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/gitops.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod/releases/payments.yaml",
        },
    )[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "chart-source-not-in-snapshot"


def test_flux_helm_release_discovers_local_chart_values_files():
    release = _fixture("flux-helmrelease-values-files.yaml")
    resource = _flux_resource()
    resource["metadata"]["annotations"]["meta.helm.sh/release-name"] = "prod-payments"

    source = discover_flux_helm_release(
        release,
        [resource],
        [_fixture("flux-gitrepository-gitops.yaml")],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/gitops.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod/releases/payments.yaml",
        },
    )[0]

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer_config["chart_values_files"] == ["charts/payments/values-prod.yaml"]
    assert source.renderer_config["chart_ignore_missing_values_files"] is False


def test_flux_helm_release_resolves_only_git_inventoried_values_from_in_the_release_namespace():
    release = _fixture("flux-helmrelease-values-from.yaml")
    kustomization = _fixture("flux-kustomization.yaml")
    kustomization["spec"]["path"] = "./clusters/prod"
    kustomization["spec"]["sourceRef"] = {"kind": "GitRepository", "name": "gitops"}
    kustomization["status"]["inventory"]["entries"] = [
        {"id": "flux-system_payments_helm.toolkit.fluxcd.io_HelmRelease", "v": "v2"},
        {"id": "flux-system_payments-values__ConfigMap", "v": "v1"},
        {"id": "flux-system_payments-secret-values__Secret", "v": "v1"},
    ]
    git_repository = _fixture("flux-gitrepository-gitops.yaml")
    resource = _flux_resource()
    resource["metadata"]["annotations"]["meta.helm.sh/release-name"] = "prod-payments"
    values_sources = discover_flux_helm_release_values_sources(
        release,
        [kustomization],
        [git_repository],
        provider_id="github-main",
    )

    source = discover_flux_helm_release(
        release,
        [resource],
        [git_repository],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/gitops.git",
            "ref": "main",
            "revision": "a" * 40,
            "path": "clusters/prod",
        },
        git_managed_values=values_sources,
    )[0]

    assert source.status is BindingStatus.RESOLVED
    assert [entry["git_managed"] for entry in source.renderer_config["values_from"]] == [True, True]
    assert [entry["namespace"] for entry in source.renderer_config["values_from"]] == [
        "flux-system",
        "flux-system",
    ]
    assert all(entry["path"] == "clusters/prod" for entry in source.renderer_config["values_from"])
    assert "data" not in json.dumps(source.model_dump(mode="json"))
    assert "250m" not in json.dumps(source.model_dump(mode="json"))


def test_flux_helm_release_does_not_infer_its_manifest_owner_from_valuesfrom_files():
    release = _fixture("flux-helmrelease-values-from.yaml")
    kustomization = _fixture("flux-kustomization.yaml")
    kustomization["spec"]["path"] = "./clusters/prod"
    kustomization["spec"]["sourceRef"] = {"kind": "GitRepository", "name": "gitops"}
    kustomization["status"]["inventory"]["entries"] = [
        {"id": "flux-system_payments-values__ConfigMap", "v": "v1"},
        {"id": "flux-system_payments-secret-values__Secret", "v": "v1"},
    ]
    git_repository = _fixture("flux-gitrepository-gitops.yaml")
    values_sources = discover_flux_helm_release_values_sources(
        release,
        [kustomization],
        [git_repository],
        provider_id="github-main",
    )
    resource = _flux_resource()
    resource["metadata"]["annotations"]["meta.helm.sh/release-name"] = "prod-payments"

    source = discover_flux_helm_release(
        release,
        [resource],
        [git_repository],
        cluster="prod",
        provider_id="github-main",
        git_managed_values=values_sources,
    )[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "helmrelease-not-in-git"


def test_flux_helm_release_blocks_runtime_only_values_from():
    release = _fixture("flux-helmrelease.yaml")
    source = discover_flux_helm_release(
        release,
        [_flux_resource()],
        [_fixture("flux-helmrepository.yaml")],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/platform.git",
            "ref": "main",
            "path": "clusters/prod/releases/payments.yaml",
        },
    )[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "values-source-not-in-git"


def test_flux_helm_release_blocks_without_explicit_git_owner_for_inline_values():
    release = _fixture("flux-helmrelease.yaml")
    release["spec"]["valuesFrom"] = []

    source = discover_flux_helm_release(
        release,
        [_flux_resource()],
        [_fixture("flux-helmrepository.yaml")],
        cluster="prod",
        provider_id="github-main",
    )[0]

    assert source.status is BindingStatus.BLOCKED
    assert source.reason == "helmrelease-not-in-git"


@pytest.mark.parametrize(
    ("section", "key", "value", "expected_count"),
    [
        ("labels", "app.kubernetes.io/managed-by", "other", 0),
        ("annotations", "meta.helm.sh/release-namespace", "other", 0),
        ("metadata", "namespace", "other", 0),
    ],
)
def test_flux_helm_release_requires_helm_managed_release_identity(
    section: str,
    key: str,
    value: str,
    expected_count: int,
):
    resource = _flux_resource()
    if section == "metadata":
        resource["metadata"][key] = value
    else:
        resource["metadata"][section][key] = value

    sources = discover_flux_helm_release(
        _fixture("flux-helmrelease.yaml"),
        [resource],
        [_fixture("flux-helmrepository.yaml")],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "repo_url": "https://github.com/acme/platform.git",
            "ref": "main",
            "path": "clusters/prod/releases/payments.yaml",
        },
    )

    assert len(sources) == expected_count


def test_argo_tracking_id_fallback_reads_the_managed_live_resource():
    application = {
        "metadata": {"name": "payments-prod", "namespace": "argocd"},
        "spec": {
            "destination": {"namespace": "prod"},
            "source": {
                "repoURL": "https://github.com/acme/apps.git",
                "targetRevision": "main",
                "path": "apps/payments",
            },
        },
        "status": {"sync": {"revision": "a" * 40}},
    }
    live_resource = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "payments-api",
            "namespace": "prod",
            "annotations": {"argocd.argoproj.io/tracking-id": "payments-prod:apps/Deployment:prod/payments-api"},
        },
    }

    sources = _argo(application, live_resources=[live_resource])

    assert len(sources) == 1
    assert (sources[0].workload_kind, sources[0].workload_name, sources[0].namespace) == (
        "Deployment",
        "payments-api",
        "prod",
    )
    assert sources[0].evidence["tracking_id_fallback"] is True
    assert sources[0].status is BindingStatus.RESOLVED
