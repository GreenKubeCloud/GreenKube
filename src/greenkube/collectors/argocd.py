"""Argo CD repository binding discovery."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from greenkube.models.repository_binding import BindingSource, RepositoryBinding


class ArgoCDCollector:
    """Extract workload-to-repository bindings from Argo CD Applications."""

    priority = 300

    def collect(self, applications: Iterable[Mapping[str, Any]]) -> list[RepositoryBinding]:
        bindings: list[RepositoryBinding] = []
        for application in applications:
            metadata = application.get("metadata") or {}
            spec = application.get("spec") or {}
            source = spec.get("source") or {}
            repo_url = source.get("repoURL")
            if not isinstance(repo_url, str) or not repo_url.strip():
                continue
            destination = spec.get("destination") or {}
            namespace = destination.get("namespace") or metadata.get("namespace") or "default"
            targets = self._targets(application)
            for target in targets:
                bindings.append(
                    RepositoryBinding(
                        cluster=str(destination.get("name") or destination.get("server") or ""),
                        namespace=str(target.get("namespace") or namespace),
                        workload_kind=str(target.get("kind") or "Application"),
                        workload_name=str(target.get("name") or metadata.get("name") or ""),
                        repo_url=repo_url.strip(),
                        path=source.get("path"),
                        branch=source.get("targetRevision"),
                        source=BindingSource.ARGOCD,
                        priority=self.priority,
                        confidence=0.95,
                        evidence={"application": metadata.get("name")},
                    )
                )
        return bindings

    @staticmethod
    def _targets(application: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        status = application.get("status") or {}
        resources = status.get("resources")
        if isinstance(resources, list) and resources:
            return [resource for resource in resources if isinstance(resource, Mapping) and resource.get("name")]
        metadata = application.get("metadata") or {}
        return [{"kind": "Application", "name": metadata.get("name"), "namespace": metadata.get("namespace")}]


ArgoCDDiscovery = ArgoCDCollector
