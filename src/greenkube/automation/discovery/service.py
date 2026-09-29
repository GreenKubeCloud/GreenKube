"""Combine GitOps discovery sources and resolve their bindings."""

from __future__ import annotations

from collections.abc import Iterable

from greenkube.automation.bindings.annotations import binding_from_annotations
from greenkube.automation.bindings.resolver import RepositoryBindingResolver
from greenkube.models.repository_binding import BindingResolution, RepositoryBinding


class RepositoryBindingDiscovery:
    """Resolve controller-derived bindings, using annotations only as fallback."""

    def __init__(self, resolver: RepositoryBindingResolver | None = None):
        self.resolver = resolver or RepositoryBindingResolver()

    def resolve(
        self,
        bindings: Iterable[RepositoryBinding],
        *,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        annotations: dict[str, str] | None = None,
        cluster: str = "",
    ) -> BindingResolution:
        candidates = list(bindings)
        annotation = binding_from_annotations(
            namespace=namespace,
            workload_kind=workload_kind,
            workload_name=workload_name,
            annotations=annotations,
            cluster=cluster,
        )
        if annotation:
            candidates.append(annotation)
        return self.resolver.resolve(candidates)
