"""Flux source and kustomization repository binding discovery."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from greenkube.models.repository_binding import BindingSource, RepositoryBinding


class FluxCollector:
    """Extract bindings from Flux Kustomization and GitRepository objects."""

    priority = 200

    def collect(
        self,
        kustomizations: Iterable[Mapping[str, Any]],
        sources: Iterable[Mapping[str, Any]] = (),
    ) -> list[RepositoryBinding]:
        source_urls = {(item.get("metadata") or {}).get("name"): self._url(item) for item in sources if self._url(item)}
        bindings: list[RepositoryBinding] = []
        for resource in kustomizations:
            metadata = resource.get("metadata") or {}
            spec = resource.get("spec") or {}
            source_ref = spec.get("sourceRef") or {}
            repo_url = source_urls.get(source_ref.get("name"))
            if not repo_url:
                continue
            namespace = str(metadata.get("namespace") or "default")
            target = spec.get("targetNamespace") or namespace
            bindings.append(
                RepositoryBinding(
                    namespace=str(target),
                    workload_kind="Kustomization",
                    workload_name=str(metadata.get("name") or ""),
                    repo_url=repo_url,
                    path=spec.get("path"),
                    branch=self._branch(resource),
                    source=BindingSource.FLUX,
                    priority=self.priority,
                    confidence=0.9,
                    evidence={"kustomization": metadata.get("name"), "source": source_ref.get("name")},
                )
            )
        return bindings

    @staticmethod
    def _url(source: Mapping[str, Any]) -> str | None:
        return (source.get("spec") or {}).get("url")

    @staticmethod
    def _branch(resource: Mapping[str, Any]) -> str | None:
        return ((resource.get("spec") or {}).get("sourceRef") or {}).get("branch")


FluxDiscovery = FluxCollector
