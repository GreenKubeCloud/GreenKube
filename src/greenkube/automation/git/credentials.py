"""Resolve Git provider credentials from injected Kubernetes Secret access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from greenkube.automation.git.base import GitProviderError
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig

_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_SECRET_KEY = re.compile(r"[A-Za-z0-9._-]+")


class SecretResolverError(RuntimeError):
    """Raised by a Kubernetes Secret resolver when a referenced value is unavailable."""


class KubernetesSecretResolver(Protocol):
    """Read a decoded value from a Kubernetes Secret.

    Implementations return ``None`` for a missing Secret/key and raise
    :class:`SecretResolverError` for resolution failures. Error messages must
    not contain Secret data.
    """

    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str | None: ...


class GitProviderCredentialError(GitProviderError):
    """Raised when a provider Secret reference or its credential is unusable."""

    code = "provider-credentials-unavailable"


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A validated reference to one key in a Kubernetes Secret."""

    namespace: str | None
    name: str
    key: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _is_dns_subdomain(self.name):
            raise ValueError("Invalid Kubernetes Secret name.")
        if self.namespace is not None and (not isinstance(self.namespace, str) or not _is_dns_label(self.namespace)):
            raise ValueError("Invalid Kubernetes namespace.")
        if (
            not isinstance(self.key, str)
            or len(self.key) > 253
            or self.key in {".", ".."}
            or _SECRET_KEY.fullmatch(self.key) is None
        ):
            raise ValueError("Invalid Kubernetes Secret key.")

    @classmethod
    def from_provider_config(cls, provider: GitOpsProviderConfig) -> SecretReference:
        """Build a validated reference without reading credential material."""
        if (
            not isinstance(provider.secret_name, str)
            or not isinstance(provider.secret_key, str)
            or (provider.secret_namespace is not None and not isinstance(provider.secret_namespace, str))
        ):
            raise GitProviderCredentialError("Git provider Secret reference is missing or invalid.")
        try:
            return cls(
                namespace=provider.secret_namespace,
                name=provider.secret_name,
                key=provider.secret_key,
            )
        except ValueError:
            raise GitProviderCredentialError("Git provider Secret reference is missing or invalid.") from None


class GitProviderCredentialResolver:
    """Resolve a provider token only through the injected Kubernetes Secret port."""

    def __init__(self, secret_resolver: KubernetesSecretResolver):
        self._secret_resolver = secret_resolver

    async def resolve(self, provider: GitOpsProviderConfig) -> str:
        """Resolve a non-empty token from the provider's referenced Secret key."""
        reference = SecretReference.from_provider_config(provider)
        secret_identity = f"{reference.namespace + '/' if reference.namespace else ''}{reference.name}[{reference.key}]"
        try:
            token = await self._secret_resolver.resolve_secret(
                namespace=reference.namespace,
                name=reference.name,
                key=reference.key,
            )
        except SecretResolverError:
            raise GitProviderCredentialError(
                f"Git provider Secret reference {secret_identity!r} could not be resolved; "
                "verify the Secret exists and its read-only mount or namespaced reader is configured."
            ) from None
        if not isinstance(token, str) or not token.strip():
            raise GitProviderCredentialError(
                f"Git provider Secret reference {secret_identity!r} is missing or empty; "
                "verify the configured Secret key."
            )
        return token.strip()


def _is_dns_label(value: str) -> bool:
    return len(value) <= 63 and _DNS_LABEL.fullmatch(value) is not None


def _is_dns_subdomain(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    return all(_is_dns_label(label) for label in value.split("."))
