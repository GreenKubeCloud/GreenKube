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
            "GREENKUBE_GENERATE_POSTGRES_PASSWORD": "TRUE",
            "GREENKUBE_POSTGRES_USER": "u",
            "GREENKUBE_POSTGRES_DATABASE": "d",
            "GREENKUBE_POSTGRES_HOST": "h",
            "GREENKUBE_POSTGRES_PORT": "",
            "GREENKUBE_POSTGRES_PVC": "data-h-0",
        }
    )

    assert settings.generate_api_key and settings.generate_postgres_password
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
