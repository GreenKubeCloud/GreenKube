"""Shared pure-mapping helpers for GitOps discovery adapters."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from greenkube.gitops.contracts import (
    BindingStatus,
    ControllerKind,
    RendererKind,
    WorkloadSource,
    canonical_repository_identity,
)
from greenkube.gitops.sha import GIT_OBJECT_SHA

_ARTIFACT_SHA_PATTERN = re.compile(
    r"^(?:.*@sha1:([0-9a-fA-F]{40})|.*@sha256:([0-9a-fA-F]{64})|sha256:([0-9a-fA-F]{64}))$"
)

WORKLOAD_KINDS = frozenset(
    {
        "CronJob",
        "DaemonSet",
        "Deployment",
        "Job",
        "Pod",
        "ReplicaSet",
        "Rollout",
        "StatefulSet",
    }
)
SUPPORTED_WORKLOAD_KINDS = frozenset({"DaemonSet", "Deployment", "StatefulSet"})


def as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def positive_generation(value: Any) -> int | None:
    """Return a Kubernetes object generation only when it is a positive integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def flux_release_name(controller_name: str, spec: Mapping[str, Any]) -> str:
    """Resolve the release name using Flux's ``GetReleaseName`` composition."""
    configured = text(spec.get("releaseName"))
    if configured is not None:
        return configured
    target_namespace = text(spec.get("targetNamespace"))
    return f"{target_namespace}-{controller_name}" if target_namespace is not None else controller_name


def safe_repo_path(value: Any, *, allow_root: bool = True) -> str | None:
    """Return a canonical repository-relative POSIX path, rejecting traversal."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if "\\" in value or value.startswith("/") or any(ord(character) < 32 for character in value):
        return None
    path = PurePosixPath(value)
    if any(part == ".." for part in path.parts):
        return None
    parts = [part for part in path.parts if part not in {"", "."}]
    canonical = "/".join(parts)
    if not canonical:
        return "." if allow_root else None
    return canonical


def join_repo_path(base: str, relative: Any, *, allow_root: bool = False) -> str | None:
    normalized_base = safe_repo_path(base)
    normalized_relative = safe_repo_path(relative, allow_root=allow_root)
    if normalized_base is None or normalized_relative is None:
        return None
    if normalized_relative == ".":
        return normalized_base if allow_root else None
    combined = normalized_relative if normalized_base == "." else f"{normalized_base}/{normalized_relative}"
    return safe_repo_path(combined, allow_root=allow_root)


def immutable_revision(value: str | None) -> str | None:
    if value is None:
        return None
    if GIT_OBJECT_SHA.fullmatch(value):
        return value
    match = _ARTIFACT_SHA_PATTERN.fullmatch(value)
    if match is None:
        return None
    return next(group for group in match.groups() if group is not None)


def controller_evidence(
    controller_kind: ControllerKind,
    spec: Mapping[str, Any],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """Return normalized controller suspend/health evidence without guessing."""
    if controller_kind is ControllerKind.ARGOCD:
        health = as_mapping(status.get("health")) or {}
        sync = as_mapping(status.get("sync")) or {}
        health_status = text(health.get("status"))
        sync_status = text(sync.get("status"))
        healthy = (
            health_status == "Healthy"
            if health_status is not None
            else (sync_status == "Synced" if sync_status is not None else None)
        )
        return {"controller_suspended": False, "controller_healthy": healthy}

    if controller_kind in {ControllerKind.FLUX_KUSTOMIZATION, ControllerKind.FLUX_HELM_RELEASE}:
        suspended = spec.get("suspend")
        conditions = status.get("conditions")
        ready: bool | None = None
        if isinstance(conditions, list):
            for condition in conditions:
                normalized = as_mapping(condition)
                if normalized is None or text(normalized.get("type")) != "Ready":
                    continue
                ready_status = text(normalized.get("status"))
                if ready_status == "True":
                    ready = True
                elif ready_status == "False":
                    ready = False
                break
        return {
            "controller_suspended": suspended if type(suspended) is bool else None,
            "controller_healthy": ready,
        }

    return {}


def workload_source(
    *,
    cluster: str,
    workload_namespace: str,
    workload_kind: str,
    workload_name: str,
    provider_id: str,
    repo_url: str,
    ref: str,
    revision: str | None,
    path: str,
    controller_kind: ControllerKind,
    controller_name: str,
    controller_namespace: str,
    controller_uid: str | None = None,
    controller_generation: int | None = None,
    source_uid: str | None = None,
    source_generation: int | None = None,
    source_revision: str | None,
    renderer: RendererKind,
    renderer_config: dict[str, Any],
    status: BindingStatus,
    evidence: dict[str, Any],
    reason: str | None = None,
    observed_at: datetime | None = None,
    identity_salt: str = "",
) -> WorkloadSource:
    identity = "\0".join(
        (
            controller_kind.value,
            cluster,
            controller_namespace,
            controller_name,
            workload_namespace,
            workload_kind,
            workload_name,
            provider_id,
            canonical_repository_identity(repo_url),
            ref,
            path,
            identity_salt,
        )
    )
    candidate_id = f"{controller_kind.value}-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
    confidence = {
        BindingStatus.RESOLVED: 0.95,
        BindingStatus.AMBIGUOUS: 0.4,
        BindingStatus.UNRESOLVED: 0.2,
        BindingStatus.BLOCKED: 0.1,
        BindingStatus.STALE: 0.2,
    }[status]
    values: dict[str, Any] = {
        "id": candidate_id,
        "cluster": text(cluster) or "unknown",
        "namespace": workload_namespace,
        "workload_kind": workload_kind,
        "workload_name": workload_name,
        "provider_id": text(provider_id) or "unresolved",
        "repo_url": text(repo_url) or "unresolved",
        "ref": text(ref) or "HEAD",
        "revision": revision,
        "path": path,
        "controller_kind": controller_kind,
        "controller_name": text(controller_name) or "unknown",
        "controller_namespace": text(controller_namespace) or "default",
        "controller_uid": text(controller_uid),
        "controller_generation": positive_generation(controller_generation),
        "source_uid": text(source_uid),
        "source_generation": positive_generation(source_generation),
        "source_revision": source_revision,
        "renderer": renderer,
        "renderer_config": renderer_config,
        "confidence": confidence,
        "status": status,
        "evidence": evidence,
        "reason": reason,
    }
    if observed_at is not None:
        values["observed_at"] = observed_at
    return WorkloadSource(**values)


# -- destination cluster (production readiness: delivery safety) ---------------------------
#: Binding reason of a controller that deploys to another cluster than the local one.
DESTINATION_REMOTE_REASON = "destination_remote"
#: Argo CD's name of the cluster it runs in.
ARGO_IN_CLUSTER_NAME = "in-cluster"
#: Addresses of the local API server as Argo CD and kubeconfigs write them.
_LOCAL_SERVERS = frozenset(
    {
        "https://kubernetes.default.svc",
        "https://kubernetes.default.svc:443",
        "https://kubernetes.default.svc.cluster.local",
        "https://kubernetes.default.svc.cluster.local:443",
        "https://kubernetes.default",
        "https://kubernetes.default:443",
    }
)


def normalize_server_url(value: str) -> str:
    """A Kubernetes API server URL compared case-insensitively and without a trailing slash."""
    return value.strip().rstrip("/").lower()


def argo_destination_is_local(destination: Mapping[str, Any] | None, local_aliases: Iterable[str] = ()) -> bool:
    """Whether an Argo CD ``spec.destination`` addresses the cluster GreenKube runs in.

    ``server: https://kubernetes.default.svc`` and ``name: in-cluster`` are Argo CD's own
    cluster; ``local_aliases`` adds the server URLs or cluster names under which this
    cluster is registered in Argo CD (``GITOPS_LOCAL_CLUSTER_ALIASES``). A destination
    with neither ``server`` nor ``name`` is invalid for Argo CD (it refuses to sync it);
    it is treated as local so partial objects keep their usual binding checks.
    """
    destination = destination or {}
    aliases = [alias.strip() for alias in local_aliases if isinstance(alias, str) and alias.strip()]
    server = text(destination.get("server"))
    if server is not None:
        servers = _LOCAL_SERVERS | {normalize_server_url(alias) for alias in aliases if "://" in alias}
        return normalize_server_url(server) in servers
    name = text(destination.get("name"))
    if name is not None:
        return name == ARGO_IN_CLUSTER_NAME or name in {alias for alias in aliases if "://" not in alias}
    return True


def flux_targets_remote_cluster(spec: Mapping[str, Any] | None) -> bool:
    """Whether a Flux Kustomization or HelmRelease applies to another cluster (``spec.kubeConfig``)."""
    return (spec or {}).get("kubeConfig") is not None


def destination_evidence(destination: Mapping[str, Any]) -> dict[str, Any]:
    """The destination fields shown with a ``destination_remote`` binding (no credentials)."""
    return {key: destination[key] for key in ("server", "name", "namespace") if text(destination.get(key)) is not None}


def flux_destination_evidence(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The remote target of a Flux object (the kubeconfig Secret name, never its content)."""
    kube_config = as_mapping(spec.get("kubeConfig")) or {}
    secret = as_mapping(kube_config.get("secretRef")) or {}
    name = text(secret.get("name"))
    return {"kube_config_secret": name} if name is not None else {"kube_config": "configured"}


def mark_destination_remote(candidate: WorkloadSource, destination: Mapping[str, Any]) -> WorkloadSource:
    """``candidate`` blocked because its controller deploys to another cluster: it never binds.

    ``destination`` is the evidence shown with it (:func:`destination_evidence`,
    :func:`flux_destination_evidence`).
    """
    evidence = dict(candidate.evidence)
    evidence["destination"] = dict(destination)
    evidence[DESTINATION_REMOTE_REASON] = True
    return candidate.model_copy(
        update={
            "status": BindingStatus.BLOCKED,
            "confidence": 0.1,
            "reason": DESTINATION_REMOTE_REASON,
            "evidence": evidence,
        }
    )
