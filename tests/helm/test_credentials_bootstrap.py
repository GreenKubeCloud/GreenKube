"""Tests for the chart's credentials bootstrap hook script (roadmap N1, BUG-58).

The script runs on every ``helm upgrade`` and every Argo CD sync. It must
create the generated-credentials Secret once and never rotate a value.
"""

import base64
import email.message
import importlib.util
import io
import json
import ssl
import string
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path("helm-chart/files/credentials_bootstrap.py")
_spec = importlib.util.spec_from_file_location("greenkube_credentials_bootstrap", SCRIPT)
assert _spec is not None and _spec.loader is not None
bootstrap = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bootstrap
_spec.loader.exec_module(bootstrap)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _plain(secret: dict) -> dict[str, str]:
    return {key: base64.b64decode(value).decode() for key, value in secret.get("data", {}).items()}


class FakeApi:
    """In-memory Secret/PVC store that mimics the Kubernetes API semantics we rely on."""

    def __init__(self, secret: dict | None = None, pvcs: set[str] | None = None):
        self.secret = secret
        self.pvcs = pvcs or set()
        self.calls: list[tuple[str, dict]] = []
        self.create_conflicts = 0
        self.patch_conflicts = 0
        self.on_create_conflict: dict | None = None

    def read_secret(self, name):
        self.calls.append(("read", {"name": name}))
        if self.secret is None:
            return None
        return json.loads(json.dumps(self.secret))

    def create_secret(self, body):
        self.calls.append(("create", body))
        if self.create_conflicts:
            self.create_conflicts -= 1
            self.secret = self.on_create_conflict
            raise bootstrap.ConflictError("exists")
        self.secret = {**body, "metadata": {**body["metadata"], "resourceVersion": "1"}}

    def patch_secret(self, name, body):
        self.calls.append(("patch", body))
        assert self.secret is not None
        if self.patch_conflicts:
            self.patch_conflicts -= 1
            self._bump()
            raise bootstrap.ConflictError("changed")
        expected = body.get("metadata", {}).get("resourceVersion")
        assert expected == self.secret["metadata"]["resourceVersion"]
        self.secret["data"] = {**self.secret.get("data", {}), **body["data"]}
        self._bump()

    def _bump(self):
        metadata = self.stored["metadata"]
        metadata["resourceVersion"] = str(int(metadata["resourceVersion"]) + 1)

    @property
    def stored(self) -> dict:
        assert self.secret is not None
        return self.secret

    def pvc_exists(self, name):
        self.calls.append(("pvc", {"name": name}))
        return name in self.pvcs

    def writes(self):
        return [call for call in self.calls if call[0] in {"create", "patch"}]


def _settings(**overrides):
    values = {
        "namespace": "greenkube",
        "secret_name": "greenkube",
        "release": "greenkube",
        "generate_api_key": True,
        "generate_postgres_password": True,
        "postgres_user": "greenkube",
        "postgres_database": "greenkube",
        "postgres_host": "greenkube-postgres",
        "postgres_port": "5432",
        "postgres_pvc": "data-greenkube-postgres-0",
    }
    values.update(overrides)
    return bootstrap.Settings(**values)


LEGACY_DSN = "postgresql://greenkube:pw@greenkube-postgres:5432/greenkube"


def _legacy_secret(**data: str) -> dict:
    """A Secret as written by chart 0.3.x (lookup + randAlphaNum hook)."""
    return {
        "metadata": {
            "name": "greenkube",
            "resourceVersion": "7",
            "labels": {"app.kubernetes.io/instance": "greenkube"},
            "annotations": {"helm.sh/hook": "pre-install,pre-upgrade"},
        },
        "data": {key: _b64(value) for key, value in data.items()},
    }


def test_creates_the_secret_with_generated_credentials_when_absent():
    api = FakeApi()

    message = bootstrap.ensure_credentials(api, _settings())

    assert message.startswith("Created Secret greenkube")
    data = _plain(api.stored)
    assert set(data) == {"GREENKUBE_API_KEY", "POSTGRES_PASSWORD", "DB_CONNECTION_STRING"}
    assert len(data["GREENKUBE_API_KEY"]) == 64
    assert len(data["POSTGRES_PASSWORD"]) == 24
    assert set(data["GREENKUBE_API_KEY"] + data["POSTGRES_PASSWORD"]) <= set(string.ascii_letters + string.digits)
    assert data["DB_CONNECTION_STRING"] == (
        f"postgresql://greenkube:{data['POSTGRES_PASSWORD']}@greenkube-postgres:5432/greenkube"
    )


def test_generates_the_scrape_token_once_without_a_bootstrap_api_key():
    """Session modes no longer generate the full-admin bootstrap key, only a metrics:read scrape token."""
    api = FakeApi()
    settings = _settings(generate_api_key=False, generate_metrics_token=True)

    bootstrap.ensure_credentials(api, settings)
    first = _plain(api.stored)
    bootstrap.ensure_credentials(api, settings)

    assert "GREENKUBE_API_KEY" not in first
    assert len(first["GREENKUBE_METRICS_TOKEN"]) == 64
    assert _plain(api.stored) == first
    assert len(api.writes()) == 1


def test_an_upgrade_adds_the_scrape_token_and_keeps_the_existing_bootstrap_key():
    legacy = _legacy_secret(GREENKUBE_API_KEY="legacy-key", POSTGRES_PASSWORD="pw", DB_CONNECTION_STRING=LEGACY_DSN)
    api = FakeApi(secret=legacy, pvcs={"data-greenkube-postgres-0"})

    bootstrap.ensure_credentials(api, _settings(generate_api_key=False, generate_metrics_token=True))

    ((_, patch),) = api.writes()
    assert set(patch["data"]) == {"GREENKUBE_METRICS_TOKEN"}
    assert _plain(api.stored)["GREENKUBE_API_KEY"] == "legacy-key"


def test_created_secret_is_not_tracked_by_helm_or_argo_cd():
    api = FakeApi()

    bootstrap.ensure_credentials(api, _settings())

    metadata = api.stored["metadata"]
    assert "app.kubernetes.io/instance" not in metadata["labels"]
    assert metadata["labels"]["app.kubernetes.io/managed-by"] != "Helm"
    assert not any(key.startswith(("meta.helm.sh/", "helm.sh/")) for key in metadata["annotations"])
    assert "argocd.argoproj.io/tracking-id" not in metadata["annotations"]
    assert metadata["annotations"]["argocd.argoproj.io/sync-options"] == "Prune=false"
    assert metadata["annotations"]["greenkube.cloud/helm-release"] == "greenkube"


def test_rerunning_on_every_sync_never_rotates_credentials():
    api = FakeApi()
    bootstrap.ensure_credentials(api, _settings())
    first = _plain(api.stored)

    for _ in range(3):
        message = bootstrap.ensure_credentials(api, _settings())
        assert "nothing to do" in message

    assert _plain(api.stored) == first
    assert len(api.writes()) == 1


def test_upgrade_from_a_lookup_generated_secret_keeps_every_value():
    legacy = _legacy_secret(
        GREENKUBE_API_KEY="legacy-key",
        POSTGRES_PASSWORD="legacy-password",
        DB_CONNECTION_STRING="postgresql://greenkube:legacy-password@greenkube-postgres:5432/greenkube",
        PROMETHEUS_URL="http://prometheus:9090",
    )
    api = FakeApi(secret=legacy, pvcs={"data-greenkube-postgres-0"})

    bootstrap.ensure_credentials(api, _settings())

    assert api.writes() == []
    assert _plain(api.stored)["POSTGRES_PASSWORD"] == "legacy-password"
    assert api.stored["metadata"]["annotations"] == {"helm.sh/hook": "pre-install,pre-upgrade"}


def test_adds_only_missing_keys_and_reuses_the_existing_password():
    legacy = _legacy_secret(POSTGRES_PASSWORD="kept", ELECTRICITY_MAPS_TOKEN="token")
    api = FakeApi(secret=legacy, pvcs={"data-greenkube-postgres-0"})

    bootstrap.ensure_credentials(api, _settings())

    ((_, patch),) = api.writes()
    assert patch["metadata"] == {"resourceVersion": "7"}
    added = {key: base64.b64decode(value).decode() for key, value in patch["data"].items()}
    assert set(added) == {"GREENKUBE_API_KEY", "DB_CONNECTION_STRING"}
    assert added["DB_CONNECTION_STRING"] == "postgresql://greenkube:kept@greenkube-postgres:5432/greenkube"
    assert _plain(api.stored)["ELECTRICITY_MAPS_TOKEN"] == "token"


@pytest.mark.parametrize(
    "overrides",
    [{"postgres_user": "app"}, {"postgres_database": "carbon"}, {"postgres_host": "other-postgres"}],
    ids=["user", "database", "host"],
)
def test_rebuilds_a_stale_connection_string_from_the_stored_password(overrides):
    # 0.3.x re-rendered DB_CONNECTION_STRING on every upgrade; the bootstrap keeps
    # that derived key in sync with the current values but never touches the password.
    legacy = _legacy_secret(
        GREENKUBE_API_KEY="key",
        POSTGRES_PASSWORD="kept",
        DB_CONNECTION_STRING="postgresql://greenkube:kept@greenkube-postgres:5432/greenkube",
    )
    api = FakeApi(secret=legacy, pvcs={"data-greenkube-postgres-0"})
    settings = _settings(**overrides)

    message = bootstrap.ensure_credentials(api, settings)

    ((_, patch),) = api.writes()
    assert set(patch["data"]) == {"DB_CONNECTION_STRING"}
    assert message == "Updated Secret greenkube: DB_CONNECTION_STRING"
    data = _plain(api.stored)
    assert data["POSTGRES_PASSWORD"] == "kept"
    assert data["GREENKUBE_API_KEY"] == "key"
    assert data["DB_CONNECTION_STRING"] == bootstrap.connection_string(settings, "kept")
    # The next sync finds nothing to change.
    assert "nothing to do" in bootstrap.ensure_credentials(api, settings)
    assert len(api.writes()) == 1


def test_connection_string_is_left_alone_when_the_password_is_supplied():
    legacy = _legacy_secret(GREENKUBE_API_KEY="key", DB_CONNECTION_STRING="custom")
    api = FakeApi(secret=legacy)

    bootstrap.ensure_credentials(api, _settings(generate_postgres_password=False))

    assert api.writes() == []


@pytest.mark.parametrize("secret", [None, _legacy_secret(GREENKUBE_API_KEY="key")], ids=["absent", "no-password"])
def test_refuses_a_new_password_for_a_retained_postgres_volume(secret):
    api = FakeApi(secret=secret, pvcs={"data-greenkube-postgres-0"})

    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.ensure_credentials(api, _settings())

    assert "kubectl -n greenkube delete pvc data-greenkube-postgres-0" in str(error.value)
    assert "postgres.auth.password" in str(error.value)
    assert api.writes() == []


def test_volume_check_only_runs_when_a_password_must_be_generated():
    api = FakeApi(pvcs={"data-greenkube-postgres-0"})

    bootstrap.ensure_credentials(api, _settings(generate_postgres_password=False))

    assert set(_plain(api.stored)) == {"GREENKUBE_API_KEY"}
    assert ("pvc", {"name": "data-greenkube-postgres-0"}) not in api.calls


def test_without_persistence_a_password_is_generated_without_a_volume_check():
    api = FakeApi()

    bootstrap.ensure_credentials(api, _settings(postgres_pvc=""))

    assert "POSTGRES_PASSWORD" in _plain(api.stored)
    assert all(call[0] != "pvc" for call in api.calls)


def test_creates_an_empty_secret_when_every_credential_is_supplied():
    api = FakeApi()

    message = bootstrap.ensure_credentials(api, _settings(generate_api_key=False, generate_postgres_password=False))

    assert message == "Created Secret greenkube with keys: none"
    assert api.stored["data"] == {}


def test_a_concurrent_create_is_resolved_without_overwriting():
    winner = _legacy_secret(
        GREENKUBE_API_KEY="winner",
        POSTGRES_PASSWORD="winner-pw",
        DB_CONNECTION_STRING="postgresql://greenkube:winner-pw@greenkube-postgres:5432/greenkube",
    )
    api = FakeApi()
    api.create_conflicts = 1
    api.on_create_conflict = winner

    message = bootstrap.ensure_credentials(api, _settings())

    assert "nothing to do" in message
    assert _plain(api.stored)["GREENKUBE_API_KEY"] == "winner"


def test_a_concurrent_patch_is_retried_with_the_new_resource_version():
    api = FakeApi(secret=_legacy_secret(POSTGRES_PASSWORD="pw", DB_CONNECTION_STRING=LEGACY_DSN))
    api.patch_conflicts = 1

    bootstrap.ensure_credentials(api, _settings())

    patches = [body for kind, body in api.writes() if kind == "patch"]
    assert [patch["metadata"]["resourceVersion"] for patch in patches] == ["7", "8"]
    assert "GREENKUBE_API_KEY" in _plain(api.stored)


def test_gives_up_after_repeated_conflicts():
    api = FakeApi(secret=_legacy_secret(POSTGRES_PASSWORD="pw", DB_CONNECTION_STRING=LEGACY_DSN))
    api.patch_conflicts = 10

    with pytest.raises(bootstrap.BootstrapError, match="concurrently"):
        bootstrap.ensure_credentials(api, _settings(), attempts=3)


def test_settings_from_env():
    settings = bootstrap.Settings.from_env(
        {
            "GREENKUBE_NAMESPACE": "gk",
            "GREENKUBE_SECRET_NAME": "gk-secret",
            "GREENKUBE_RELEASE": "gk",
            "GREENKUBE_GENERATE_API_KEY": "true",
            "GREENKUBE_GENERATE_METRICS_TOKEN": "true",
            "GREENKUBE_GENERATE_POSTGRES_PASSWORD": "TRUE",
            "GREENKUBE_POSTGRES_USER": "u",
            "GREENKUBE_POSTGRES_DATABASE": "d",
            "GREENKUBE_POSTGRES_HOST": "h",
            "GREENKUBE_POSTGRES_PORT": "",
            "GREENKUBE_POSTGRES_PVC": "data-h-0",
        }
    )

    assert settings.generate_api_key and settings.generate_postgres_password
    assert settings.generate_metrics_token
    assert settings.postgres_port == "5432"
    assert settings.postgres_pvc == "data-h-0"


@pytest.mark.parametrize(
    "env",
    [
        {"GREENKUBE_SECRET_NAME": "gk"},
        {"GREENKUBE_NAMESPACE": "gk"},
        {"GREENKUBE_NAMESPACE": "gk", "GREENKUBE_SECRET_NAME": "gk", "GREENKUBE_GENERATE_POSTGRES_PASSWORD": "true"},
    ],
)
def test_settings_from_env_rejects_incomplete_configuration(env):
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.Settings.from_env(env)


def test_main_reports_errors_without_a_traceback(capsys):
    assert bootstrap.main({}) == 1
    assert "credentials bootstrap failed" in capsys.readouterr().err


def test_main_requires_an_in_cluster_environment(capsys):
    env = {"GREENKUBE_NAMESPACE": "gk", "GREENKUBE_SECRET_NAME": "gk"}

    assert bootstrap.main(env) == 1
    assert "KUBERNETES_SERVICE_HOST" in capsys.readouterr().err


def _http_error(code: int, message: str = "") -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps({"message": message}).encode())
    return urllib.error.HTTPError("https://k8s/x", code, "status", email.message.Message(), body)


@pytest.fixture
def client(monkeypatch):
    requests = []
    responses: list = []

    def fake_urlopen(request, timeout, context):
        requests.append(request)
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(json.dumps(outcome).encode() if outcome is not None else b"")

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fake_urlopen)
    api = bootstrap.InClusterApi("gk", "https://10.0.0.1:443", "sa-token", context=ssl.create_default_context())
    return api, requests, responses


def test_in_cluster_api_reads_creates_and_patches(client):
    api, requests, responses = client
    responses.extend([{"metadata": {"name": "gk"}}, {}, {}, _http_error(404)])

    assert api.read_secret("gk") == {"metadata": {"name": "gk"}}
    api.create_secret({"metadata": {"name": "gk"}})
    api.patch_secret("gk", {"data": {}})
    assert api.pvc_exists("data-gk-postgres-0") is False

    assert [(r.get_method(), r.full_url) for r in requests] == [
        ("GET", "https://10.0.0.1:443/api/v1/namespaces/gk/secrets/gk"),
        ("POST", "https://10.0.0.1:443/api/v1/namespaces/gk/secrets"),
        ("PATCH", "https://10.0.0.1:443/api/v1/namespaces/gk/secrets/gk"),
        ("GET", "https://10.0.0.1:443/api/v1/namespaces/gk/persistentvolumeclaims/data-gk-postgres-0"),
    ]
    assert requests[0].get_header("Authorization") == "Bearer sa-token"
    assert requests[2].get_header("Content-type") == "application/merge-patch+json"


def test_in_cluster_api_maps_errors(client):
    api, _, responses = client
    responses.extend([_http_error(404), _http_error(409), _http_error(403, "secrets is forbidden")])

    assert api.read_secret("gk") is None
    with pytest.raises(bootstrap.ConflictError):
        api.create_secret({})
    with pytest.raises(bootstrap.BootstrapError, match="HTTP 403: secrets is forbidden"):
        api.patch_secret("gk", {})


def test_in_cluster_api_wraps_ipv6_hosts(monkeypatch, tmp_path):
    (tmp_path / "token").write_text("tok\n")
    monkeypatch.setattr(bootstrap, "_SERVICE_ACCOUNT_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap.ssl, "create_default_context", lambda cafile: None)

    api = bootstrap.InClusterApi.from_environment("gk", {"KUBERNETES_SERVICE_HOST": "fd00::1"})

    assert api.base_url == "https://[fd00::1]:443"
    assert api.token == "tok"


# --------------------------------------------------------------------------- #
# Initial admin Secret (F14)                                                   #
# --------------------------------------------------------------------------- #


class MultiSecretApi:
    def __init__(self, secrets: dict[str, dict] | None = None):
        self.secrets = dict(secrets or {})
        self.created: list[dict] = []
        self.conflict_on_create = False

    def read_secret(self, name):
        return self.secrets.get(name)

    def create_secret(self, body):
        if self.conflict_on_create:
            raise bootstrap.ConflictError("exists")
        self.created.append(body)
        self.secrets[body["metadata"]["name"]] = body

    def patch_secret(self, name, body):  # pragma: no cover - must never be called for the admin Secret
        raise AssertionError("the initial admin Secret is never patched by the hook")

    def pvc_exists(self, name):
        return False


def _admin_settings(**overrides):
    return _settings(initial_admin_secret="gk-initial-admin", initial_admin_username="admin", **overrides)


def test_creates_the_initial_admin_secret_once_with_a_random_password():
    api = MultiSecretApi()

    message = bootstrap.ensure_initial_admin_secret(api, _admin_settings(), token=lambda length: "p" * length)

    assert "Created Secret gk-initial-admin" in message
    body = api.created[0]
    assert _plain(body) == {"username": "admin", "password": "p" * 24}
    assert body["metadata"]["labels"]["app.kubernetes.io/component"] == "initial-admin"
    assert "app.kubernetes.io/instance" not in body["metadata"]["labels"]
    assert "p" * 24 not in message


def test_never_reissues_an_existing_or_erased_initial_admin_secret():
    tombstone = {"metadata": {"name": "gk-initial-admin"}, "data": {}}
    api = MultiSecretApi({"gk-initial-admin": tombstone})

    message = bootstrap.ensure_initial_admin_secret(api, _admin_settings())

    assert "already exists" in message
    assert api.created == []


def test_a_concurrently_created_initial_admin_secret_is_kept():
    api = MultiSecretApi()
    api.conflict_on_create = True

    assert "concurrently" in bootstrap.ensure_initial_admin_secret(api, _admin_settings())


def test_initial_admin_only_mode_needs_no_release_secret():
    settings = bootstrap.Settings.from_env(
        {"GREENKUBE_NAMESPACE": "gk", "GREENKUBE_INITIAL_ADMIN_SECRET": "gk-initial-admin"}
    )

    assert settings.secret_name == ""
    assert settings.initial_admin_secret == "gk-initial-admin"
    assert settings.initial_admin_username == "admin"


# --------------------------------------------------------------------------- #
# Scrape token copy for a ServiceMonitor namespace                             #
# --------------------------------------------------------------------------- #
# Prometheus Operator reads a ServiceMonitor's bearer token Secret in the
# ServiceMonitor's own namespace, so with monitoring.serviceMonitor.namespace
# set elsewhere the hook mirrors the scrape token there.


class CopyApi:
    """Secret store of one namespace with optimistic concurrency."""

    def __init__(self, secrets: dict[str, dict[str, str]] | None = None):
        self.secrets: dict[str, dict] = {
            name: {"metadata": {"name": name, "resourceVersion": "1"}, "data": {k: _b64(v) for k, v in data.items()}}
            for name, data in (secrets or {}).items()
        }
        self.writes: list[tuple[str, dict]] = []
        self.patch_conflicts = 0

    def read_secret(self, name):
        secret = self.secrets.get(name)
        return json.loads(json.dumps(secret)) if secret else None

    def create_secret(self, body):
        self.writes.append(("create", body))
        name = body["metadata"]["name"]
        if name in self.secrets:
            raise bootstrap.ConflictError("exists")
        self.secrets[name] = {**body, "metadata": {**body["metadata"], "resourceVersion": "1"}}

    def patch_secret(self, name, body):
        self.writes.append(("patch", body))
        stored = self.secrets[name]
        if self.patch_conflicts:
            self.patch_conflicts -= 1
            stored["metadata"]["resourceVersion"] = str(int(stored["metadata"]["resourceVersion"]) + 1)
            raise bootstrap.ConflictError("changed")
        assert body["metadata"]["resourceVersion"] == stored["metadata"]["resourceVersion"]
        stored["data"] = {**stored.get("data", {}), **body["data"]}

    def pvc_exists(self, name):  # pragma: no cover - unused by the copy
        return False


def _copy_settings(**overrides):
    values = {
        "scrape_token_namespace": "monitoring",
        "scrape_token_secret": "gk-scrape-token",
        "scrape_token_source": "greenkube",
        "scrape_token_source_key": "GREENKUBE_METRICS_TOKEN",
    }
    values.update(overrides)
    return _settings(**values)


def test_copies_the_scrape_token_into_the_servicemonitor_namespace():
    source = CopyApi({"greenkube": {"GREENKUBE_METRICS_TOKEN": "tok", "POSTGRES_PASSWORD": "pw"}})
    target = CopyApi()

    message = bootstrap.ensure_scrape_token_copy(source, target, _copy_settings())

    assert message == "Created Secret gk-scrape-token in namespace monitoring with the scrape token"
    body = target.secrets["gk-scrape-token"]
    assert _plain(body) == {"token": "tok"}, "only the scrape token leaves the release namespace"
    labels = body["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == "scrape-token"
    assert "app.kubernetes.io/instance" not in labels
    assert source.writes == []


def test_the_scrape_token_copy_is_stable_across_upgrades():
    source = CopyApi({"greenkube": {"GREENKUBE_METRICS_TOKEN": "tok"}})
    target = CopyApi({"gk-scrape-token": {"token": "tok"}})

    message = bootstrap.ensure_scrape_token_copy(source, target, _copy_settings())

    assert "already holds the scrape token" in message
    assert target.writes == []


def test_the_scrape_token_copy_follows_a_rotated_source():
    source = CopyApi({"greenkube": {"GREENKUBE_METRICS_TOKEN": "new"}})
    target = CopyApi({"gk-scrape-token": {"token": "old"}})
    target.patch_conflicts = 1

    message = bootstrap.ensure_scrape_token_copy(source, target, _copy_settings())

    assert message == "Updated Secret gk-scrape-token in namespace monitoring with the scrape token"
    assert _plain(target.secrets["gk-scrape-token"]) == {"token": "new"}


@pytest.mark.parametrize("secrets", [{}, {"greenkube": {"GREENKUBE_API_KEY": "admin"}}])
def test_refuses_to_copy_a_missing_scrape_token(secrets):
    target = CopyApi()

    with pytest.raises(bootstrap.BootstrapError, match="GREENKUBE_METRICS_TOKEN"):
        bootstrap.ensure_scrape_token_copy(CopyApi(secrets), target, _copy_settings())
    assert target.writes == []


def test_settings_from_env_reads_the_scrape_token_copy():
    settings = bootstrap.Settings.from_env(
        {
            "GREENKUBE_NAMESPACE": "gk",
            "GREENKUBE_SCRAPE_TOKEN_NAMESPACE": "monitoring",
            "GREENKUBE_SCRAPE_TOKEN_SECRET": "gk-scrape-token",
            "GREENKUBE_SCRAPE_TOKEN_SOURCE": "my-creds",
        }
    )

    assert settings.secret_name == ""
    assert settings.scrape_token_namespace == "monitoring"
    assert settings.scrape_token_secret == "gk-scrape-token"
    assert settings.scrape_token_source == "my-creds"
    assert settings.scrape_token_source_key == "GREENKUBE_METRICS_TOKEN"


def test_settings_from_env_requires_a_complete_scrape_token_copy():
    with pytest.raises(bootstrap.BootstrapError, match="GREENKUBE_SCRAPE_TOKEN"):
        bootstrap.Settings.from_env(
            {
                "GREENKUBE_NAMESPACE": "gk",
                "GREENKUBE_SECRET_NAME": "gk",
                "GREENKUBE_SCRAPE_TOKEN_NAMESPACE": "monitoring",
            }
        )


def test_main_creates_the_credentials_before_copying_the_scrape_token(monkeypatch, capsys):
    namespaces = {"gk": CopyApi(), "monitoring": CopyApi()}

    class Api(CopyApi):
        def __init__(self, namespace):
            self.__dict__ = namespaces[namespace].__dict__

        def in_namespace(self, namespace):
            return Api(namespace)

    monkeypatch.setattr(bootstrap.InClusterApi, "from_environment", classmethod(lambda cls, ns, env: Api(ns)))
    env = {
        "GREENKUBE_NAMESPACE": "gk",
        "GREENKUBE_SECRET_NAME": "gk",
        "GREENKUBE_GENERATE_METRICS_TOKEN": "true",
        "GREENKUBE_SCRAPE_TOKEN_NAMESPACE": "monitoring",
        "GREENKUBE_SCRAPE_TOKEN_SECRET": "gk-scrape-token",
        "GREENKUBE_SCRAPE_TOKEN_SOURCE": "gk",
    }

    assert bootstrap.main(env) == 0

    generated = _plain(namespaces["gk"].secrets["gk"])["GREENKUBE_METRICS_TOKEN"]
    assert _plain(namespaces["monitoring"].secrets["gk-scrape-token"]) == {"token": generated}
    assert "namespace monitoring" in capsys.readouterr().out


def test_in_cluster_api_targets_another_namespace(client):
    api, requests, responses = client
    responses.append(None)

    assert api.in_namespace("monitoring").read_secret("gk-scrape-token") == {}

    assert requests[0].full_url == "https://10.0.0.1:443/api/v1/namespaces/monitoring/secrets/gk-scrape-token"
    assert api.namespace == "gk"
