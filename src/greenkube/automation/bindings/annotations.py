"""Last-resort annotation binding extraction."""

from __future__ import annotations

from typing import Any

from greenkube.models.repository_binding import BindingSource, RepositoryBinding

ANNOTATION_REPO = "greenkube.cloud/repository"
ANNOTATION_PATH = "greenkube.cloud/repository-path"
ANNOTATION_BRANCH = "greenkube.cloud/repository-branch"


def binding_from_annotations(
    *,
    namespace: str,
    workload_kind: str,
    workload_name: str,
    annotations: dict[str, Any] | None,
    cluster: str = "",
) -> RepositoryBinding | None:
    """Build a low-priority binding from explicit workload annotations."""

    annotations = annotations or {}
    repo_url = annotations.get(ANNOTATION_REPO)
    if not isinstance(repo_url, str) or not repo_url.strip():
        return None
    return RepositoryBinding(
        cluster=cluster,
        namespace=namespace,
        workload_kind=workload_kind,
        workload_name=workload_name,
        repo_url=repo_url.strip(),
        path=annotations.get(ANNOTATION_PATH),
        branch=annotations.get(ANNOTATION_BRANCH),
        source=BindingSource.ANNOTATION,
        priority=100,
        confidence=0.5,
        evidence={"annotations": [ANNOTATION_REPO]},
    )
