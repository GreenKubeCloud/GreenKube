"""The default credentials Secret is read through the Kubernetes API (get/watch on one name)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

from greenkube.automation.git.default_credentials import (
    STATE_ABSENT,
    STATE_PRESENT,
    STATE_UNKNOWN,
    DefaultCredentialsSecret,
)
from greenkube.automation.git.k8s_secret_resolver import MountedSecretFileResolver
from greenkube.automation.readiness import CredentialsInspector
from tests.automation.test_readiness import (  # pyrefly: ignore[missing-import]
    GITHUB_REPO,
    FakeGitOpsRepository,
    _readiness,
    _write_token,
)

SECRET = "greenkube-git-credentials"


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeCoreV1:
    """Answers ``read_namespaced_secret`` for one Secret; records every call."""

    def __init__(self) -> None:
        self.data: dict[str, str] | None = None
        self.error: int | None = None
        self.calls: list[tuple[str, str]] = []

    def create(self, **values: str) -> None:
        self.data = {key: base64.b64encode(value.encode()).decode() for key, value in values.items()}

    async def read_namespaced_secret(self, *, name: str, namespace: str) -> Any:
        self.calls.append((namespace, name))
        if self.error is not None:
            raise ApiError(self.error)
        if self.data is None:
            raise ApiError(404)
        return SimpleNamespace(data=dict(self.data), metadata=SimpleNamespace(name=name))


def _store(api: FakeCoreV1, clock: list[float] | None = None) -> DefaultCredentialsSecret:
    now = clock if clock is not None else [0.0]

    async def factory() -> FakeCoreV1:
        return api

    return DefaultCredentialsSecret(SECRET, "greenkube", api_factory=factory, monotonic=lambda: now[0])


@pytest.mark.asyncio
async def test_creating_the_secret_activates_delivery_on_the_next_tick_without_the_volume(tmp_path):
    """Regression: delivery waited for the kubelet to refresh the optional projected volume."""
    api = FakeCoreV1()
    store = _store(api)
    repository = FakeGitOpsRepository([GITHUB_REPO])
    readiness = _readiness(repository, tmp_path)
    readiness._credentials = CredentialsInspector(tmp_path, api_secret=store)

    snapshot = await readiness.run_once()
    assert snapshot.capabilities["delivery"].reason == "credentials_missing"

    # kubectl create secret generic greenkube-git-credentials --from-literal=token=...: no file mounted yet.
    api.create(token="t0k3n", host="github.com")
    snapshot = await readiness.run_once()

    assert snapshot.capabilities["delivery"].active
    assert repository.providers["inferred-github.com"].enabled is True
    assert api.calls == [("greenkube", SECRET), ("greenkube", SECRET)]

    # Deleting it is seen at once too, even while the stale mounted copy still exists.
    _write_token(tmp_path)
    api.data = None
    snapshot = await readiness.run_once()
    assert snapshot.capabilities["delivery"].state == "dormant"


@pytest.mark.asyncio
async def test_resolver_prefers_the_api_view_and_falls_back_to_the_mount(tmp_path):
    api = FakeCoreV1()
    clock = [0.0]
    store = _store(api, clock)
    resolver = MountedSecretFileResolver(tmp_path, api_secret=store)
    _write_token(tmp_path, value="from-volume")

    # RBAC missing (older chart): the mounted volume is used.
    api.error = 403
    assert await resolver.resolve_secret(namespace=None, name=SECRET, key="token") == "from-volume"
    assert store.state == STATE_UNKNOWN

    clock[0] = 60.0
    api.error = None
    api.create(token="from-api")
    assert await resolver.resolve_secret(namespace=None, name=SECRET, key="token") == "from-api"
    assert store.state == STATE_PRESENT
    # Cached for max_age: no second get.
    assert await resolver.resolve_secret(namespace=None, name=SECRET, key="token") == "from-api"
    assert len(api.calls) == 2

    # Other Secrets (explicit providers) are never read through the API.
    other = tmp_path / "github-token" / "token"
    other.parent.mkdir()
    other.write_text("explicit")
    assert await resolver.resolve_secret(namespace=None, name="github-token", key="token") == "explicit"
    assert len(api.calls) == 2

    clock[0] = 120.0
    api.data = None
    assert await resolver.resolve_secret(namespace=None, name=SECRET, key="token") is None
    assert store.state == STATE_ABSENT


def test_watch_events_update_the_view_of_the_one_secret():
    store = _store(FakeCoreV1())
    encoded = base64.b64encode(b"abc").decode()

    store.apply_event({"type": "ADDED", "object": {"metadata": {"name": "another"}, "data": {"token": encoded}}})
    assert store.state == STATE_UNKNOWN
    store.apply_event({"type": "ADDED", "object": {"metadata": {"name": SECRET}, "data": {"token": encoded}}})
    assert (store.state, store.value("token"), store.has("appId")) == (STATE_PRESENT, b"abc", False)
    assert store.changed.is_set()
    store.apply_event({"type": "DELETED", "object": {"metadata": {"name": SECRET}}})
    assert (store.state, store.value("token")) == (STATE_ABSENT, None)


@pytest.mark.asyncio
async def test_watch_selects_the_secret_by_name_and_never_lists(monkeypatch):
    seen: dict[str, Any] = {}

    class FakeWatch:
        def stream(self, function, **kwargs):
            seen["function"] = function
            seen["kwargs"] = kwargs

            async def events():
                yield {"type": "ADDED", "object": {"metadata": {"name": SECRET}, "data": {}}}

            return events()

        def stop(self) -> None:
            seen["stopped"] = True

    monkeypatch.setattr("kubernetes_asyncio.watch.Watch", FakeWatch)

    class Api(FakeCoreV1):
        async def list_namespaced_secret(self, **kwargs):  # only ever called by the watch
            raise AssertionError("list must not be called directly")

    api = Api()
    store = _store(api)
    await store.watch_once(timeout_seconds=5)

    assert seen["function"] == api.list_namespaced_secret
    assert seen["kwargs"] == {
        "namespace": "greenkube",
        "field_selector": f"metadata.name={SECRET}",
        "timeout_seconds": 5,
    }
    assert seen["stopped"] is True
    assert store.state == STATE_PRESENT


def test_secret_values_never_appear_in_repr():
    store = _store(FakeCoreV1())
    store.apply_event(
        {
            "type": "ADDED",
            "object": {"metadata": {"name": SECRET}, "data": {"token": base64.b64encode(b"s3cr3t").decode()}},
        }
    )
    assert "s3cr3t" not in repr(store._snapshot)
