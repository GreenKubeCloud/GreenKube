from greenkube.collectors.argocd import ArgoCDCollector
from greenkube.collectors.flux import FluxCollector
from greenkube.models.repository_binding import BindingSource


def test_argocd_collector_maps_application_resources() -> None:
    bindings = ArgoCDCollector().collect(
        [
            {
                "metadata": {"name": "platform", "namespace": "argocd"},
                "spec": {
                    "source": {"repoURL": "https://example.test/platform.git", "path": "clusters/prod"},
                    "destination": {"server": "https://kubernetes.default.svc", "namespace": "production"},
                },
                "status": {"resources": [{"kind": "Deployment", "name": "api", "namespace": "production"}]},
            }
        ]
    )
    assert len(bindings) == 1
    assert bindings[0].source == BindingSource.ARGOCD
    assert bindings[0].workload_name == "api"


def test_flux_collector_maps_git_repository_to_kustomization() -> None:
    bindings = FluxCollector().collect(
        [
            {
                "metadata": {"name": "platform", "namespace": "flux-system"},
                "spec": {
                    "path": "./clusters/prod",
                    "targetNamespace": "production",
                    "sourceRef": {"kind": "GitRepository", "name": "platform"},
                },
            }
        ],
        [{"metadata": {"name": "platform"}, "spec": {"url": "https://example.test/platform.git"}}],
    )
    assert len(bindings) == 1
    assert bindings[0].source == BindingSource.FLUX
    assert bindings[0].path == "./clusters/prod"
