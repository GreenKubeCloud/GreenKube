"""Bootstrap of the first admin account (roadmap F14, Argo CD initial-admin pattern).

On a database without accounts, the API creates the user ``admin`` (setting
``AUTH_ADMIN_USERNAME``) with one of these passwords:

1. ``AUTH_ADMIN_PASSWORD``, mounted from ``auth.admin.existingSecret`` (no
   forced change: the operator chose it);
2. the random password the chart's credentials bootstrap hook wrote once to
   the Secret ``<release>-initial-admin`` (forced change at first sign-in).

The hook, not the API, creates that Secret: Kubernetes RBAC cannot restrict
``create`` to one resource name, and a pod allowed to create any Secret in its
namespace could mint service-account tokens. The API's Role is limited to
``get`` and ``patch`` on that single Secret. Once no account still uses the
initial password, the API erases the plaintext from the Secret and leaves it as
an empty tombstone, so a later hook run never issues a new initial password.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Protocol

from greenkube.auth.service import AuthService

logger = logging.getLogger(__name__)

PASSWORD_KEY = "password"
CONSUMED_ANNOTATION = "greenkube.cloud/initial-admin"
_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


class InitialAdminSecret(Protocol):
    name: str

    async def read_password(self) -> str | None:
        """The initial plaintext password, or ``None`` when absent or erased."""
        ...

    async def erase(self) -> bool:
        """Remove the plaintext password; ``True`` when something was erased."""
        ...


class KubernetesInitialAdminSecret:
    """The ``<release>-initial-admin`` Secret, read and erased through the API server."""

    def __init__(self, name: str, namespace: str) -> None:
        self.name = name
        self.namespace = namespace

    @classmethod
    def from_environment(cls, name: str, namespace_file: str = _NAMESPACE_FILE) -> KubernetesInitialAdminSecret | None:
        """Return the store when running in a cluster with a configured Secret name."""
        if not name:
            return None
        try:
            with open(namespace_file, encoding="utf-8") as handle:
                namespace = handle.read().strip()
        except OSError:
            return None
        return cls(name, namespace) if namespace else None

    async def _read(self) -> Any | None:
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio import config as k8s_config
        from kubernetes_asyncio.client.exceptions import ApiException

        k8s_config.load_incluster_config()
        async with k8s_client.ApiClient() as api_client:
            try:
                return await k8s_client.CoreV1Api(api_client).read_namespaced_secret(self.name, self.namespace)
            except ApiException as error:
                if error.status == 404:
                    return None
                raise

    async def read_password(self) -> str | None:
        secret = await self._read()
        encoded = (getattr(secret, "data", None) or {}).get(PASSWORD_KEY) if secret is not None else None
        if not encoded:
            return None
        try:
            return base64.b64decode(encoded).decode("utf-8").strip() or None
        except (ValueError, UnicodeDecodeError):
            return None

    async def erase(self) -> bool:
        if await self.read_password() is None:
            return False
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio import config as k8s_config

        k8s_config.load_incluster_config()
        body = {"data": {PASSWORD_KEY: None}, "metadata": {"annotations": {CONSUMED_ANNOTATION: "consumed"}}}
        async with k8s_client.ApiClient() as api_client:
            await k8s_client.CoreV1Api(api_client).patch_namespaced_secret(self.name, self.namespace, body)
        return True


def initial_admin_secret_from_config(cfg: Any) -> InitialAdminSecret | None:
    name = str(getattr(cfg, "AUTH_INITIAL_ADMIN_SECRET", "") or "").strip()
    return KubernetesInitialAdminSecret.from_environment(name)


async def erase_consumed_initial_password(service: AuthService, secret: InitialAdminSecret | None) -> bool:
    """Erase the initial plaintext once no account still has to change it."""
    if secret is None or await service.initial_password_pending():
        return False
    try:
        erased = await secret.erase()
    except Exception as error:  # pragma: no cover - depends on the API server
        logger.warning(
            "Could not erase the initial admin password from Secret %s (%s).", secret.name, type(error).__name__
        )
        return False
    if erased:
        logger.info("Erased the initial admin password from Secret %s.", secret.name)
    return erased


async def ensure_initial_admin(service: AuthService, cfg: Any, secret: InitialAdminSecret | None) -> str:
    """Create the first admin when no account exists; return what happened.

    Outcomes: ``exists``, ``created_from_config``, ``created_from_secret`` or
    ``no_password``. Passwords are never logged.
    """
    if await service.users_exist():
        await erase_consumed_initial_password(service, secret)
        return "exists"

    password = str(getattr(cfg, "AUTH_ADMIN_PASSWORD", "") or "").strip()
    outcome = "created_from_config"
    must_change = False
    if not password and secret is not None:
        password = await secret.read_password() or ""
        outcome = "created_from_secret"
        must_change = True
    if not password:
        logger.warning(
            "No account exists and no initial admin password is available: set auth.admin.existingSecret "
            "(AUTH_ADMIN_PASSWORD) or let the chart create the %s Secret.",
            getattr(secret, "name", "<release>-initial-admin"),
        )
        return "no_password"

    created = await service.create_first_admin(password, must_change_password=must_change)
    if created is None:
        return "exists"
    logger.info("Created the initial admin account %r.", created.username)
    return outcome
