from greenkube.automation.bindings.annotations import binding_from_annotations
from greenkube.automation.bindings.resolver import RepositoryBindingResolver
from greenkube.models.repository_binding import BindingSource, BindingStatus, RepositoryBinding


def _binding(source: BindingSource, priority: int, repo_url: str) -> RepositoryBinding:
    return RepositoryBinding(
        namespace="production",
        workload_kind="Deployment",
        workload_name="api",
        repo_url=repo_url,
        source=source,
        priority=priority,
    )


def test_controller_binding_wins_over_annotation() -> None:
    annotation = binding_from_annotations(
        namespace="production",
        workload_kind="Deployment",
        workload_name="api",
        annotations={"greenkube.cloud/repository": "https://example.test/annotation.git"},
    )
    assert annotation is not None
    result = RepositoryBindingResolver().resolve(
        [_binding(BindingSource.ARGOCD, 300, "https://example.test/controller.git"), annotation]
    )
    assert result.status == BindingStatus.RESOLVED
    assert result.binding is not None
    assert result.binding.source == BindingSource.ARGOCD


def test_equal_priority_different_repositories_are_ambiguous() -> None:
    result = RepositoryBindingResolver().resolve(
        [
            _binding(BindingSource.ARGOCD, 300, "https://example.test/one.git"),
            _binding(BindingSource.ARGOCD, 300, "https://example.test/two.git"),
        ]
    )
    assert result.status == BindingStatus.AMBIGUOUS
    assert result.binding is None


def test_missing_candidates_are_unresolved() -> None:
    result = RepositoryBindingResolver().resolve([])
    assert result.status == BindingStatus.UNRESOLVED


def test_coverage_excludes_ambiguous_and_missing_workloads() -> None:
    resolver = RepositoryBindingResolver()
    candidate = _binding(BindingSource.ARGOCD, 300, "https://example.test/one.git")
    assert resolver.coverage([candidate.workload_key, ("production", "Deployment", "worker")], [candidate]) == 0.5
