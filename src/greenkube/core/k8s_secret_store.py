"""
Read-only access to externally mounted secrets.

GreenKube never writes runtime configuration into Kubernetes Secrets: data
source endpoints and tokens are configuration as code (Helm values and
Secrets, roadmap A13). The former ``patch_k8s_secret`` writer, and the RBAC
that allowed the API to patch the release Secret, were removed because UI
edits reached only the API process and drifted from Git (BUG-11, BUG-60).
"""

import os

_SECRET_MOUNT = "/etc/greenkube/secrets"


class ExternalSecretStore:
    """Read-only view of externally mounted secrets.

    The mounted volume is owned by the orchestrator (Kubernetes, Docker, or a
    secret manager). Runtime configuration must never attempt to mutate it.
    """

    def __init__(self, mount_path: str = _SECRET_MOUNT):
        self.mount_path = mount_path

    def read(self, key: str) -> str | None:
        """Read a mounted secret, returning ``None`` when it is not mounted."""
        path = os.path.join(self.mount_path, key)
        try:
            with open(path, encoding="utf-8") as secret_file:
                return secret_file.read().strip()
        except FileNotFoundError:
            return None
        except (PermissionError, OSError) as exc:
            raise RuntimeError(f"External secret '{key}' could not be read") from exc


external_secret_store = ExternalSecretStore()
