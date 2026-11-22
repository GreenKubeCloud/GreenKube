"""First admin bootstrap and the initial-admin Secret (roadmap F14, N11)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from greenkube.auth.bootstrap import (
    KubernetesInitialAdminSecret,
    ensure_initial_admin,
    initial_admin_secret_from_config,
)
from greenkube.auth.lifecycle import bootstrap_initial_admin
from greenkube.auth.passwords import verify_password
from greenkube.auth.service import AuthService
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.storage.sqlite.auth_repository import SQLiteAuthRepository

GENERATED = "GeneratedInitialPassword24"


class FakeSecret:
    name = "greenkube-initial-admin"

    def __init__(self, password: str | None = GENERATED):
        self.password = password
        self.erase_calls = 0

    async def read_password(self):
        return self.password

    async def erase(self):
        self.erase_calls += 1
        had = self.password is not None
        self.password = None
        return had


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bootstrap.sqlite"))
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield AuthService(SQLiteAuthRepository(manager))
    finally:
        await manager.close()


def _cfg(**values):
    return SimpleNamespace(**{"AUTH_ADMIN_PASSWORD": None, "AUTH_INITIAL_ADMIN_SECRET": "", **values})


async def test_generated_secret_creates_an_admin_that_must_change_it(service, caplog):
    secret = FakeSecret()
    with caplog.at_level(logging.DEBUG):
        outcome = await ensure_initial_admin(service, _cfg(), secret)

    admin = await service.repository.get_user_by_username("admin")
    assert outcome == "created_from_secret"
    assert admin is not None and admin.role == "admin" and admin.must_change_password is True
    assert verify_password(GENERATED, admin.password_hash)
    assert GENERATED not in caplog.text
    assert secret.erase_calls == 0  # still needed until the password is changed


async def test_existing_secret_password_needs_no_forced_change(service):
    outcome = await ensure_initial_admin(service, _cfg(AUTH_ADMIN_PASSWORD="operator-chosen-pass"), FakeSecret())

    admin = await service.repository.get_user_by_username("admin")
    assert outcome == "created_from_config"
    assert admin is not None and admin.must_change_password is False
    assert verify_password("operator-chosen-pass", admin.password_hash)


async def test_no_password_source_creates_nothing(service):
    assert await ensure_initial_admin(service, _cfg(), None) == "no_password"
    assert await ensure_initial_admin(service, _cfg(), FakeSecret(password=None)) == "no_password"
    assert await service.repository.count_users() == 0


async def test_plaintext_is_erased_once_the_initial_password_was_changed(service):
    secret = FakeSecret()
    await ensure_initial_admin(service, _cfg(), secret)
    admin = await service.repository.get_user_by_username("admin")
    assert admin is not None

    # Still pending: a restart keeps the Secret.
    assert await ensure_initial_admin(service, _cfg(), secret) == "exists"
    assert secret.password == GENERATED

    await service.change_password(admin, GENERATED, "a-new-strong-password")
    # A later start (or a hook that re-created the Secret) erases the stale plaintext.
    secret.password = "stale-password-from-a-new-hook-run"
    assert await ensure_initial_admin(service, _cfg(), secret) == "exists"
    assert secret.password is None


async def test_bootstrap_retries_and_reports_failure(service, monkeypatch):
    monkeypatch.setattr("greenkube.auth.lifecycle.BOOTSTRAP_RETRY_SECONDS", 0)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("api server unavailable")

    monkeypatch.setattr("greenkube.auth.lifecycle.ensure_initial_admin", boom)

    assert await bootstrap_initial_admin(service, attempts=2) == "failed"


async def test_bootstrap_waits_for_the_schema_beyond_the_retry_budget(service, monkeypatch):
    """F11: the controller migrates; a slow 0.3.x upgrade must not exhaust the bootstrap.

    The schema stays "migrating" for far longer than attempts x retry delay; the
    bootstrap waits without a deadline and only then creates the admin.
    """
    sleeps: list[float] = []

    async def fast_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("greenkube.auth.lifecycle.asyncio.sleep", fast_sleep)
    checks = 0

    async def schema_ready() -> bool:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise ConnectionError("database restarting")
        return checks > 40

    created: list[str] = []

    async def ensure(*_args, **_kwargs):
        assert checks > 40, "the admin is only created once the schema is current"
        created.append("admin")
        return "created"

    monkeypatch.setattr("greenkube.auth.lifecycle.ensure_initial_admin", ensure)

    assert await bootstrap_initial_admin(service, attempts=2, schema_ready=schema_ready) == "created"
    assert created == ["admin"]
    assert len(sleeps) == 40
    assert max(sleeps) == 30.0
    # Waiting lasted far longer than the 2 x 30 s retry budget.
    assert sum(sleeps) > 2 * 30


def test_secret_store_needs_a_name_and_a_cluster(tmp_path):
    namespace_file = tmp_path / "namespace"
    assert KubernetesInitialAdminSecret.from_environment("", str(namespace_file)) is None
    assert KubernetesInitialAdminSecret.from_environment("gk-initial-admin", str(namespace_file)) is None
    namespace_file.write_text("greenkube\n", encoding="utf-8")

    store = KubernetesInitialAdminSecret.from_environment("gk-initial-admin", str(namespace_file))

    assert store is not None and store.namespace == "greenkube" and store.name == "gk-initial-admin"
    assert initial_admin_secret_from_config(SimpleNamespace(AUTH_INITIAL_ADMIN_SECRET="")) is None
