"""Least-privilege Kubernetes Secret readers for Git provider credentials."""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import aiofiles

from greenkube.automation.git.credentials import (
    KubernetesSecretResolver,
    SecretReference,
    SecretResolverError,
)
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig

DEFAULT_PROVIDER_SECRET_MOUNT = Path("/etc/greenkube/git-providers")
_MAX_SECRET_BYTES = 1024 * 1024


class MountedSecretFileResolver(KubernetesSecretResolver):
    """Read provider tokens only from explicitly mounted Secret key files.

    Mount each referenced Secret read-only using this layout:
    ``<mount>/<secret-name>/<key>``. The default mount is
    ``/etc/greenkube/git-providers``, matching the chart's projected Secret
    keys. This resolver never queries the Kubernetes API.
    """

    def __init__(self, mount_path: str | Path | None = None) -> None:
        configured_mount = os.getenv("GITOPS_SECRET_MOUNT")
        selected_mount = mount_path if mount_path is not None else configured_mount
        self.mount_path = Path(selected_mount) if selected_mount else DEFAULT_PROVIDER_SECRET_MOUNT

    def path_for_reference(self, namespace: str | None, name: str, key: str) -> Path:
        reference = SecretReference(namespace=namespace, name=name, key=key)
        parts = [reference.namespace] if reference.namespace else []
        parts.extend((reference.name, reference.key))
        root = self.mount_path
        path = root.joinpath(*parts)
        try:
            resolved = path.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, RuntimeError, ValueError):
            raise SecretResolverError("Git provider Secret mount resolves outside its configured mount root.") from None
        return path if path.is_absolute() else Path.cwd() / path

    def validate_provider_mount(self, provider: GitOpsProviderConfig) -> None:
        """Fail fast with a safe mount instruction when a configured key is absent."""
        try:
            reference = SecretReference.from_provider_config(provider)
        except (ValueError, RuntimeError):
            raise SecretResolverError("Git provider Secret reference is missing or invalid.") from None
        path = self.path_for_reference(reference.namespace, reference.name, reference.key)
        try:
            with path.open("rb") as secret_file:
                secret_file.read(0)
        except FileNotFoundError:
            namespace = f"{reference.namespace}/" if reference.namespace else ""
            raise SecretResolverError(
                f"Git provider Secret {namespace}{reference.name!s} key {reference.key!r} is not mounted. "
                f"Mount that key read-only at {path} or inject a namespaced Kubernetes Secret reader."
            ) from None
        except (OSError, PermissionError):
            raise SecretResolverError(
                f"Git provider Secret key file at {path} is not readable; check its read-only mount permissions."
            ) from None

    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str | None:
        """Read a single mounted key without logging or retaining its value."""
        try:
            path = self.path_for_reference(namespace, name, key)
        except (ValueError, SecretResolverError):
            raise SecretResolverError("Git provider Secret reference is invalid.") from None

        try:
            async with aiofiles.open(path, "rb") as secret_file:
                value = await secret_file.read(_MAX_SECRET_BYTES + 1)
        except FileNotFoundError:
            return None
        except (OSError, PermissionError):
            raise SecretResolverError("Git provider Secret key file could not be read.") from None

        if len(value) > _MAX_SECRET_BYTES:
            raise SecretResolverError("Git provider Secret key file exceeds the supported size limit.")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretResolverError("Git provider Secret key file is not valid UTF-8.") from None


class CoreV1SecretApi(Protocol):
    """The namespaced CoreV1 read operation used by the optional API resolver."""

    async def read_namespaced_secret(self, *, name: str, namespace: str) -> Any: ...


class KubernetesCoreV1SecretResolver(KubernetesSecretResolver):
    """Resolve only explicitly referenced Secrets through an injected CoreV1 API client.

    No Kubernetes client is created automatically. Any RBAC must be scoped to
    the named Secrets and namespaces configured by the caller.
    """

    def __init__(self, core_v1_api: CoreV1SecretApi, *, default_namespace: str | None = None) -> None:
        self._core_v1_api = core_v1_api
        self._default_namespace = default_namespace

    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str | None:
        try:
            reference = SecretReference(namespace=namespace, name=name, key=key)
        except ValueError:
            raise SecretResolverError("Git provider Secret reference is invalid.") from None
        resolved_namespace = reference.namespace or self._default_namespace
        if not resolved_namespace:
            raise SecretResolverError(
                "Git provider Secret namespace is missing; configure secret_namespace or a default namespace."
            )

        try:
            secret = await self._core_v1_api.read_namespaced_secret(
                name=reference.name,
                namespace=resolved_namespace,
            )
        except Exception:
            raise SecretResolverError("The configured namespaced Git provider Secret could not be read.") from None

        if isinstance(secret, Mapping):
            data = secret.get("data")
        else:
            data = getattr(secret, "data", None)
        if not isinstance(data, Mapping):
            return None
        encoded = data.get(reference.key)
        if not isinstance(encoded, str):
            return None
        try:
            value = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise SecretResolverError("The configured Git provider Secret key is not valid base64 data.") from None
        if len(value) > _MAX_SECRET_BYTES:
            raise SecretResolverError("The configured Git provider Secret key exceeds the supported size limit.")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretResolverError("The configured Git provider Secret key is not valid UTF-8.") from None
