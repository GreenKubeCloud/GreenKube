"""Create GreenKube's generated credentials once, outside the Helm release.

The chart runs this script in a pre-install/pre-upgrade hook Job (an Argo CD
PreSync hook). It creates the Secret named by GREENKUBE_SECRET_NAME when it is
absent and only ever adds missing keys afterwards, so running it on every
upgrade or sync never rotates a credential. Rendering the chart therefore stays
a pure function of the values (roadmap N1, BUG-58).

DB_CONNECTION_STRING is derived, not generated: like chart 0.3.x, which
re-rendered it on every upgrade, it is rebuilt from the stored password when the
PostgreSQL user, database, host or port in the values change.

Generated keys:
  GREENKUBE_METRICS_TOKEN                   the Prometheus scrape token (metrics:read
                                            only), when secrets.metricsToken is empty;
  GREENKUBE_API_KEY                         the deprecated bootstrap admin key, only
                                            for config.apiAuthMode=api_key when
                                            secrets.apiKey is empty (an existing key
                                            is kept, never removed);
  POSTGRES_PASSWORD, DB_CONNECTION_STRING   for the bundled PostgreSQL when
                                            postgres.auth.password is empty;
  LEDGER_CHAIN_KEY                          the key of the savings ledger hash
                                            chain (LED-05), when
                                            secrets.ledgerChainKey is empty. It is
                                            never rotated: entries chained with it
                                            only verify with it. LEDGER_CHAIN_KEY_SINCE
                                            (UTC creation time, the anchor outside the
                                            database) is written with it, once, and
                                            never overwritten.

With GREENKUBE_INITIAL_ADMIN_SECRET set, the script also creates that Secret
once with a random first-admin password (keys ``username`` and ``password``,
roadmap F14). An existing Secret is never modified: the API erases its password
after the first sign-in and keeps it as an empty tombstone, so a later hook run
never issues a second initial password. GREENKUBE_SECRET_NAME may be empty
(secrets.existingSecret) when only the initial-admin Secret is needed.

With GREENKUBE_SCRAPE_TOKEN_NAMESPACE set (monitoring.serviceMonitor.namespace
outside the release namespace), the script also mirrors the scrape token of
Secret GREENKUBE_SCRAPE_TOKEN_SOURCE into Secret GREENKUBE_SCRAPE_TOKEN_SECRET
(key ``token``) of that namespace: Prometheus Operator reads a ServiceMonitor's
bearer token Secret in the ServiceMonitor's own namespace. Only the scrape token
is copied; the copy is rewritten only when the source changed, so it is as
stable as the source.

The Secret carries no Helm or Argo CD tracking metadata: uninstalling the
release keeps it, like the PostgreSQL volume whose password it holds.

Standard library only, so the Job runs with any GreenKube image.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import string
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

API_KEY = "GREENKUBE_API_KEY"
METRICS_TOKEN = "GREENKUBE_METRICS_TOKEN"
LEDGER_CHAIN_KEY = "LEDGER_CHAIN_KEY"
LEDGER_CHAIN_KEY_SINCE = "LEDGER_CHAIN_KEY_SINCE"
POSTGRES_PASSWORD = "POSTGRES_PASSWORD"
DB_CONNECTION_STRING = "DB_CONNECTION_STRING"

API_KEY_LENGTH = 64
METRICS_TOKEN_LENGTH = 64
LEDGER_CHAIN_KEY_LENGTH = 64
POSTGRES_PASSWORD_LENGTH = 24
INITIAL_ADMIN_PASSWORD_LENGTH = 24
_ALPHABET = string.ascii_letters + string.digits
_SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# No app.kubernetes.io/instance label and no tracking annotation: Argo CD must
# never treat this Secret as part of the application (and prune it), and Helm
# must never adopt it into the release.
SECRET_LABELS = {
    "app.kubernetes.io/name": "greenkube",
    "app.kubernetes.io/part-of": "greenkube",
    "app.kubernetes.io/component": "credentials",
    "app.kubernetes.io/managed-by": "greenkube-credentials-bootstrap",
}
SECRET_ANNOTATIONS = {
    "argocd.argoproj.io/compare-options": "IgnoreExtraneous",
    "argocd.argoproj.io/sync-options": "Prune=false",
}


class BootstrapError(RuntimeError):
    """The credentials cannot be bootstrapped safely; the message says why."""


class ConflictError(RuntimeError):
    """The Secret changed concurrently (HTTP 409)."""


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


@dataclass(frozen=True)
class Settings:
    namespace: str
    secret_name: str
    release: str = ""
    initial_admin_secret: str = ""
    initial_admin_username: str = "admin"
    generate_api_key: bool = False
    generate_metrics_token: bool = False
    generate_postgres_password: bool = False
    generate_ledger_chain_key: bool = False
    postgres_user: str = ""
    postgres_database: str = ""
    postgres_host: str = ""
    postgres_port: str = "5432"
    postgres_pvc: str = ""
    scrape_token_namespace: str = ""
    scrape_token_secret: str = ""
    scrape_token_source: str = ""
    scrape_token_source_key: str = METRICS_TOKEN

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        namespace = env.get("GREENKUBE_NAMESPACE", "").strip()
        secret_name = env.get("GREENKUBE_SECRET_NAME", "").strip()
        initial_admin_secret = env.get("GREENKUBE_INITIAL_ADMIN_SECRET", "").strip()
        scrape_token_namespace = env.get("GREENKUBE_SCRAPE_TOKEN_NAMESPACE", "").strip()
        if not namespace or not (secret_name or initial_admin_secret or scrape_token_namespace):
            raise BootstrapError(
                "GREENKUBE_NAMESPACE and GREENKUBE_SECRET_NAME, GREENKUBE_INITIAL_ADMIN_SECRET or "
                "GREENKUBE_SCRAPE_TOKEN_NAMESPACE are required"
            )
        settings = cls(
            namespace=namespace,
            secret_name=secret_name,
            release=env.get("GREENKUBE_RELEASE", "").strip(),
            initial_admin_secret=initial_admin_secret,
            initial_admin_username=env.get("GREENKUBE_INITIAL_ADMIN_USERNAME", "").strip() or "admin",
            generate_api_key=_flag(env.get("GREENKUBE_GENERATE_API_KEY")),
            generate_metrics_token=_flag(env.get("GREENKUBE_GENERATE_METRICS_TOKEN")),
            generate_postgres_password=_flag(env.get("GREENKUBE_GENERATE_POSTGRES_PASSWORD")),
            generate_ledger_chain_key=_flag(env.get("GREENKUBE_GENERATE_LEDGER_CHAIN_KEY")),
            postgres_user=env.get("GREENKUBE_POSTGRES_USER", "").strip(),
            postgres_database=env.get("GREENKUBE_POSTGRES_DATABASE", "").strip(),
            postgres_host=env.get("GREENKUBE_POSTGRES_HOST", "").strip(),
            postgres_port=env.get("GREENKUBE_POSTGRES_PORT", "5432").strip() or "5432",
            postgres_pvc=env.get("GREENKUBE_POSTGRES_PVC", "").strip(),
            scrape_token_namespace=scrape_token_namespace,
            scrape_token_secret=env.get("GREENKUBE_SCRAPE_TOKEN_SECRET", "").strip(),
            scrape_token_source=env.get("GREENKUBE_SCRAPE_TOKEN_SOURCE", "").strip(),
        )
        if settings.scrape_token_namespace and not (settings.scrape_token_secret and settings.scrape_token_source):
            raise BootstrapError(
                "GREENKUBE_SCRAPE_TOKEN_SECRET and GREENKUBE_SCRAPE_TOKEN_SOURCE are required "
                "with GREENKUBE_SCRAPE_TOKEN_NAMESPACE"
            )
        if settings.generate_postgres_password and not (
            settings.postgres_user and settings.postgres_database and settings.postgres_host
        ):
            raise BootstrapError("PostgreSQL user, database and host are required to generate its password")
        return settings


def generate_token(length: int) -> str:
    """Return a random alphanumeric token (safe inside a connection URL)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def connection_string(settings: Settings, password: str) -> str:
    return (
        f"postgresql://{settings.postgres_user}:{password}@"
        f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def missing_credentials(
    current: Mapping[str, str],
    settings: Settings,
    *,
    now: Callable[[], datetime] | None = None,
    pvc_exists: Callable[[], bool],
    token: Callable[[int], str] = generate_token,
) -> dict[str, str]:
    """Return the keys to write to ``current``.

    Generated credentials are only added, never replaced. The derived
    DB_CONNECTION_STRING is rewritten when it no longer matches the stored
    password and the current PostgreSQL settings.
    """
    additions: dict[str, str] = {}
    if settings.generate_api_key and not current.get(API_KEY):
        additions[API_KEY] = token(API_KEY_LENGTH)
    if settings.generate_metrics_token and not current.get(METRICS_TOKEN):
        additions[METRICS_TOKEN] = token(METRICS_TOKEN_LENGTH)
    if settings.generate_ledger_chain_key and not current.get(LEDGER_CHAIN_KEY):
        additions[LEDGER_CHAIN_KEY] = token(LEDGER_CHAIN_KEY_LENGTH)
        # Only with a key created now: an existing key's creation time is unknown and never guessed.
        if not current.get(LEDGER_CHAIN_KEY_SINCE):
            additions[LEDGER_CHAIN_KEY_SINCE] = (now or _utc_now)().strftime("%Y-%m-%dT%H:%M:%SZ")
    if settings.generate_postgres_password:
        password = current.get(POSTGRES_PASSWORD, "")
        if not password:
            if settings.postgres_pvc and pvc_exists():
                raise BootstrapError(_retained_volume_message(settings))
            password = token(POSTGRES_PASSWORD_LENGTH)
            additions[POSTGRES_PASSWORD] = password
        expected = connection_string(settings, password)
        if current.get(DB_CONNECTION_STRING) != expected:
            additions[DB_CONNECTION_STRING] = expected
    return additions


def _retained_volume_message(settings: Settings) -> str:
    return (
        f"The PostgreSQL volume {settings.postgres_pvc} already exists in namespace {settings.namespace}, "
        f"but Secret {settings.secret_name} has no {POSTGRES_PASSWORD}. A newly generated password cannot "
        "authenticate against the existing database. Either delete the volume for a clean install "
        f"(kubectl -n {settings.namespace} delete pvc {settings.postgres_pvc}), or provide the original "
        f"password: set postgres.auth.password, use secrets.existingSecret, or add {POSTGRES_PASSWORD} to "
        f"Secret {settings.secret_name}."
    )


def _decode(data: Mapping[str, str] | None) -> dict[str, str]:
    decoded = {}
    for key, value in (data or {}).items():
        try:
            decoded[key] = base64.b64decode(value).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            decoded[key] = ""
    return decoded


def _encode(data: Mapping[str, str]) -> dict[str, str]:
    return {key: base64.b64encode(value.encode("utf-8")).decode("ascii") for key, value in sorted(data.items())}


def new_secret(
    settings: Settings, data: Mapping[str, str], *, name: str | None = None, component: str = "credentials"
) -> dict[str, Any]:
    annotations = dict(SECRET_ANNOTATIONS)
    if settings.release:
        annotations["greenkube.cloud/helm-release"] = settings.release
    labels = {**SECRET_LABELS, "app.kubernetes.io/component": component}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name or settings.secret_name, "labels": labels, "annotations": annotations},
        "type": "Opaque",
        "data": _encode(data),
    }


def ensure_initial_admin_secret(
    api: SecretApi,
    settings: Settings,
    *,
    token: Callable[[int], str] = generate_token,
) -> str:
    """Create the initial-admin Secret once; never touch an existing one (F14)."""
    name = settings.initial_admin_secret
    if not name:
        return "No initial admin Secret requested"
    if api.read_secret(name) is not None:
        return f"Secret {name} already exists; nothing to do"
    body = new_secret(
        settings,
        {"username": settings.initial_admin_username, "password": token(INITIAL_ADMIN_PASSWORD_LENGTH)},
        name=name,
        component="initial-admin",
    )
    try:
        api.create_secret(body)
    except ConflictError:
        return f"Secret {name} was created concurrently; nothing to do"
    return f"Created Secret {name} with the initial admin password"


class SecretApi(Protocol):
    def read_secret(self, name: str) -> dict[str, Any] | None: ...

    def create_secret(self, body: dict[str, Any]) -> None: ...

    def patch_secret(self, name: str, body: dict[str, Any]) -> None: ...

    def pvc_exists(self, name: str) -> bool: ...


def ensure_credentials(
    api: SecretApi,
    settings: Settings,
    *,
    token: Callable[[int], str] = generate_token,
    attempts: int = 5,
) -> str:
    """Create the Secret if absent, add missing generated keys, and describe what happened."""
    for _ in range(attempts):
        secret = api.read_secret(settings.secret_name)
        current = _decode(secret.get("data") if secret else None)
        additions = missing_credentials(
            current,
            settings,
            pvc_exists=lambda: api.pvc_exists(settings.postgres_pvc),
            token=token,
        )
        try:
            if secret is None:
                api.create_secret(new_secret(settings, additions))
                return f"Created Secret {settings.secret_name} with keys: {', '.join(sorted(additions)) or 'none'}"
            if not additions:
                return f"Secret {settings.secret_name} already holds the generated credentials; nothing to do"
            resource_version = (secret.get("metadata") or {}).get("resourceVersion")
            patch: dict[str, Any] = {"data": _encode(additions)}
            if resource_version:
                patch["metadata"] = {"resourceVersion": resource_version}
            api.patch_secret(settings.secret_name, patch)
            return f"Updated Secret {settings.secret_name}: {', '.join(sorted(additions))}"
        except ConflictError:
            continue
    raise BootstrapError(f"Secret {settings.secret_name} kept changing concurrently; retry the upgrade")


def ensure_scrape_token_copy(
    source_api: SecretApi,
    target_api: SecretApi,
    settings: Settings,
    *,
    attempts: int = 5,
) -> str:
    """Mirror the scrape token into the ServiceMonitor namespace and describe what happened.

    ``source_api`` reads the release namespace, ``target_api`` writes the
    ServiceMonitor namespace. Only the scrape token is copied, under key ``token``.
    """
    name, namespace = settings.scrape_token_secret, settings.scrape_token_namespace
    source = source_api.read_secret(settings.scrape_token_source)
    token = _decode(source.get("data") if source else None).get(settings.scrape_token_source_key, "")
    if not token:
        raise BootstrapError(
            f"Secret {settings.scrape_token_source} in namespace {settings.namespace} has no "
            f"{settings.scrape_token_source_key} to copy into namespace {namespace} for the ServiceMonitors"
        )
    for _ in range(attempts):
        target = target_api.read_secret(name)
        try:
            if target is None:
                target_api.create_secret(new_secret(settings, {"token": token}, name=name, component="scrape-token"))
                return f"Created Secret {name} in namespace {namespace} with the scrape token"
            if _decode(target.get("data")).get("token") == token:
                return f"Secret {name} in namespace {namespace} already holds the scrape token; nothing to do"
            patch: dict[str, Any] = {"data": _encode({"token": token})}
            resource_version = (target.get("metadata") or {}).get("resourceVersion")
            if resource_version:
                patch["metadata"] = {"resourceVersion": resource_version}
            target_api.patch_secret(name, patch)
            return f"Updated Secret {name} in namespace {namespace} with the scrape token"
        except ConflictError:
            continue
    raise BootstrapError(f"Secret {name} in namespace {namespace} kept changing concurrently; retry the upgrade")


class InClusterApi:
    """Minimal Kubernetes REST client using the pod's service-account token."""

    def __init__(self, namespace: str, base_url: str, token: str, context: ssl.SSLContext, timeout: float = 10.0):
        self.namespace = namespace
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.context = context
        self.timeout = timeout

    @classmethod
    def from_environment(cls, namespace: str, env: Mapping[str, str]) -> InClusterApi:
        host = env.get("KUBERNETES_SERVICE_HOST", "")
        port = env.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise BootstrapError("KUBERNETES_SERVICE_HOST is not set; this script must run inside the cluster")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            with open(os.path.join(_SERVICE_ACCOUNT_DIR, "token"), encoding="utf-8") as token_file:
                token = token_file.read().strip()
            context = ssl.create_default_context(cafile=os.path.join(_SERVICE_ACCOUNT_DIR, "ca.crt"))
        except OSError as error:
            raise BootstrapError(f"Cannot read the service-account credentials: {error}") from error
        return cls(namespace, f"https://{host}:{port}", token, context)

    def in_namespace(self, namespace: str) -> InClusterApi:
        """The same client, scoped to another namespace."""
        return InClusterApi(namespace, self.base_url, self.token, self.context, self.timeout)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, content_type: str = "") -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", content_type or "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 409:
                raise ConflictError(path) from error
            raise BootstrapError(f"{method} {path} failed with HTTP {error.code}: {_status_message(error)}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise BootstrapError(f"{method} {path} failed: {error}") from error
        return json.loads(payload) if payload else {}

    def _path(self, resource: str, name: str = "") -> str:
        suffix = f"/{name}" if name else ""
        return f"/api/v1/namespaces/{self.namespace}/{resource}{suffix}"

    def read_secret(self, name: str) -> dict[str, Any] | None:
        return self._request("GET", self._path("secrets", name))

    def create_secret(self, body: dict[str, Any]) -> None:
        self._request("POST", self._path("secrets"), body)

    def patch_secret(self, name: str, body: dict[str, Any]) -> None:
        self._request("PATCH", self._path("secrets", name), body, "application/merge-patch+json")

    def pvc_exists(self, name: str) -> bool:
        return self._request("GET", self._path("persistentvolumeclaims", name)) is not None


def _status_message(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(error.read()).get("message") or error.reason)
    except (ValueError, OSError, AttributeError):
        return str(error.reason)


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        settings = Settings.from_env(env)
        api = InClusterApi.from_environment(settings.namespace, env)
        if settings.secret_name:
            print(f"GreenKube: {ensure_credentials(api, settings)}")
        if settings.initial_admin_secret:
            print(f"GreenKube: {ensure_initial_admin_secret(api, settings)}")
        if settings.scrape_token_namespace:
            target = api.in_namespace(settings.scrape_token_namespace)
            print(f"GreenKube: {ensure_scrape_token_copy(api, target, settings)}")
    except BootstrapError as error:
        print(f"GreenKube credentials bootstrap failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
