# src/greenkube/automation/source_resolver.py
"""Discovers the Git source of a workload through Kubernetes annotations.

v1 resolves manifests from workload annotations (primary contract):

- ``greenkube.cloud/git-repo``   (required) repository URL;
- ``greenkube.cloud/git-path``   (optional) manifest path inside the repository;
- ``greenkube.cloud/git-branch`` (optional) base branch override.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol

from greenkube.models.metrics import RecommendationRecord

logger = logging.getLogger(__name__)

ANNOTATION_REPO = "greenkube.cloud/git-repo"
ANNOTATION_PATH = "greenkube.cloud/git-path"
ANNOTATION_BRANCH = "greenkube.cloud/git-branch"

_APPS_READERS = {
    "Deployment": "read_namespaced_deployment",
    "StatefulSet": "read_namespaced_stateful_set",
    "DaemonSet": "read_namespaced_daemon_set",
}


class SourceResolutionError(RuntimeError):
    """Raised with an actionable message when no source can be resolved."""


@dataclass
class ManifestSource:
    """The Git location of a workload's manifest."""

    repo_url: str
    path: Optional[str] = None
    branch: Optional[str] = None


class AnnotationReader(Protocol):
    """Reads workload annotations, so tests can inject fakes."""

    async def read_annotations(self, namespace: str, kind: str, name: str) -> dict: ...


class K8sAnnotationReader:
    """Reads workload annotations through the Kubernetes apps API."""

    async def read_annotations(self, namespace: str, kind: str, name: str) -> dict:
        reader_name = _APPS_READERS.get(kind)
        if reader_name is None:
            raise SourceResolutionError(
                f"Workload kind '{kind}' is not supported for source discovery. "
                "Annotate a Deployment, StatefulSet or DaemonSet."
            )

        from greenkube.core.k8s_client import get_apps_v1_api

        api = await get_apps_v1_api()
        if api is None:
            raise SourceResolutionError("Kubernetes API is unavailable; cannot read workload annotations.")

        reader = getattr(api, reader_name)
        try:
            workload = await reader(name, namespace)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                raise SourceResolutionError(f"{kind} '{namespace}/{name}' was not found in the cluster.") from exc
            raise SourceResolutionError(f"Could not read {kind} '{namespace}/{name}': {exc}") from exc

        return dict(getattr(workload.metadata, "annotations", None) or {})


class SourceResolver(ABC):
    """Resolves the Git source for a recommendation target."""

    @abstractmethod
    async def resolve(self, record: RecommendationRecord) -> ManifestSource: ...


class AnnotationSourceResolver(SourceResolver):
    """Resolves manifests from ``greenkube.cloud/git-*`` workload annotations."""

    def __init__(self, reader: Optional[AnnotationReader] = None):
        self.reader = reader if reader is not None else K8sAnnotationReader()

    async def resolve(self, record: RecommendationRecord) -> ManifestSource:
        if not record.namespace or not record.owner_kind or not record.owner_name:
            raise SourceResolutionError(
                "This recommendation has no workload owner; only workload-scoped recommendations "
                "can be applied through a pull request."
            )

        annotations = await self.reader.read_annotations(record.namespace, record.owner_kind, record.owner_name)
        repo_url = annotations.get(ANNOTATION_REPO)
        if not repo_url:
            raise SourceResolutionError(
                f"{record.owner_kind} '{record.namespace}/{record.owner_name}' does not declare "
                f"'{ANNOTATION_REPO}'. Add the annotation to enable the GreenKube GitOps bot."
            )

        path = annotations.get(ANNOTATION_PATH)
        branch = annotations.get(ANNOTATION_BRANCH)
        return ManifestSource(repo_url=repo_url.strip(), path=path.strip() if path else None, branch=branch)
