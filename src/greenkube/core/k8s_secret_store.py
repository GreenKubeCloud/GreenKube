# src/greenkube/core/k8s_secret_store.py
"""
Best-effort persistence of runtime configuration overrides into the
Kubernetes Secret managed by Helm.

When the user updates service URLs or tokens from the frontend, changes
are first applied in-memory (via os.environ + Config.reload).  This
module then patches the live K8s Secret so the values survive pod
restarts without requiring a ``helm upgrade``.

The Helm ``secret.yaml`` template uses ``lookup`` to preserve any keys
already present in the Secret, which prevents a ``helm upgrade`` with
default (empty) values from wiping tokens that were set through the UI.

All errors are caught and logged — if the pod runs outside a cluster or
lacks RBAC permissions the in-memory update is still applied and the
caller is not interrupted.

Uses ``kubernetes_asyncio`` (already a GreenKube dependency) so the
patch is non-blocking inside FastAPI's async event loop.
"""

import base64
import logging
import os

logger = logging.getLogger(__name__)

# Namespace discovery: K8s injects the current namespace into every pod
# through a well-known file when the service-account token is mounted.
_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def _get_namespace() -> str:
    """Return the current pod namespace, or empty string when out-of-cluster."""
    try:
        with open(_NAMESPACE_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _get_secret_name() -> str:
    """Return the name of the GreenKube K8s Secret from env var or default."""
    return os.getenv("GREENKUBE_SECRET_NAME", "greenkube")


async def patch_k8s_secret(updates: dict[str, str]) -> bool:
    """Patch the GreenKube K8s Secret with *updates* (plain-text values).

    This is a coroutine so it integrates cleanly with FastAPI's async event
    loop without blocking.

    Args:
        updates: Mapping of environment variable name → plain-text value.
                 Example: ``{"ELECTRICITY_MAPS_TOKEN": "tok_abc123",
                             "PROMETHEUS_URL": "http://prom:9090"}``.

    Returns:
        ``True`` if the Secret was patched successfully, ``False`` otherwise.
    """
    if not updates:
        return True

    namespace = _get_namespace()
    if not namespace:
        logger.debug(
            "Running out-of-cluster — skipping K8s Secret patch for: %s",
            list(updates.keys()),
        )
        return False

    secret_name = _get_secret_name()

    try:
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio import config as k8s_config

        # load_incluster_config is synchronous (reads mounted SA token files)
        k8s_config.load_incluster_config()

        async with k8s_client.ApiClient() as api_client:
            v1 = k8s_client.CoreV1Api(api_client)

            # Build a strategic-merge patch: only update the keys we care about.
            encoded_data = {key: base64.b64encode(val.encode()).decode() for key, val in updates.items()}
            body = {"data": encoded_data}

            await v1.patch_namespaced_secret(name=secret_name, namespace=namespace, body=body)

        logger.info(
            "Persisted runtime config override(s) to K8s Secret '%s/%s': %s",
            namespace,
            secret_name,
            list(updates.keys()),
        )
        return True

    except Exception as exc:
        logger.warning(
            "Could not patch K8s Secret '%s' — runtime overrides are in-memory only: %s",
            secret_name,
            exc,
        )
        return False
