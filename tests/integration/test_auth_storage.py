"""Contract tests for accounts, sessions and API keys on SQLite and PostgreSQL (F14, F15).

Two ``DatabaseManager`` instances on one database stand for two API replicas:
a session issued by one is honoured and revoked by the other. PostgreSQL cases
run when ``GREENKUBE_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest

from greenkube.auth.service import AuthError, AuthService
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.storage.postgres.auth_repository import PostgresAuthRepository
from greenkube.storage.sqlite.auth_repository import SQLiteAuthRepository

PASSWORD = "first-admin-password"


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Backend:
    name: str
    managers: list
    clock: Clock

    def service(self, index: int = 0) -> AuthService:
        manager = self.managers[index]
        repository = PostgresAuthRepository(manager) if self.name == "postgres" else SQLiteAuthRepository(manager)
        return AuthService(repository, clock=self.clock)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def backend(request, tmp_path, monkeypatch) -> AsyncGenerator[Backend, None]:
    name = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if name == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL auth storage tests.")
        schema = f"gk_auth_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "auth.sqlite"))
        monkeypatch.setenv("DB_SCHEMA", "public")
    config.reload()
    managers = [DatabaseManager(Config()), DatabaseManager(Config())]
    for manager in managers:
        await manager.connect()
    try:
        yield Backend(name=name, managers=managers, clock=Clock(datetime.now(timezone.utc)))
    finally:
        for manager in managers:
            await manager.close()
        if name == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


async def test_first_admin_is_created_once_across_replicas(backend):
    first, second = backend.service(0), backend.service(1)

    created = await first.create_first_admin(PASSWORD, must_change_password=True)
    again = await second.create_first_admin("another-password-1", must_change_password=True)

    assert created is not None and created.username == "admin" and created.role == "admin"
    assert again is None
    assert await second.users_exist()
    assert await second.initial_password_pending()
    stored = await second.repository.get_user_by_username("admin")
    assert stored is not None and stored.must_change_password is True


async def test_duplicate_usernames_are_rejected(backend):
    service = backend.service()
    await service.create_user("ops", PASSWORD)

    with pytest.raises(AuthError) as error:
        await service.create_user("OPS", PASSWORD)

    assert error.value.code == "USERNAME_TAKEN"


async def test_a_session_issued_by_one_replica_is_valid_and_revocable_on_the_other(backend):
    """F14 acceptance: two API replicas share sessions."""
    first, second = backend.service(0), backend.service(1)
    await first.create_user("ops", PASSWORD, role="operator")

    issued = await first.login("ops", PASSWORD, remember=True, ip="10.0.0.1", user_agent="pytest")
    resolved = await second.resolve_session(issued.token)
    assert resolved is not None
    session, user = resolved
    assert user.username == "ops" and session.remember is True and session.ip == "10.0.0.1"
    assert session.absolute_expires_at - session.created_at == timedelta(days=30)

    assert await second.logout(issued.token)
    assert await first.resolve_session(issued.token) is None


async def test_idle_expiry_slides_and_purge_removes_dead_sessions(backend):
    service = backend.service()
    await service.create_user("ops", PASSWORD)
    issued = await service.login("ops", PASSWORD)

    backend.clock.now += timedelta(hours=11)
    assert await service.resolve_session(issued.token) is not None
    backend.clock.now += timedelta(hours=11)
    assert await service.resolve_session(issued.token) is not None
    backend.clock.now += timedelta(hours=12, seconds=1)
    assert await service.resolve_session(issued.token) is None

    assert await service.purge_expired_sessions() == 1
    assert await service.repository.get_session(issued.session.token_sha256) is None


async def test_password_change_revokes_sessions_and_lists_only_active_ones(backend):
    service = backend.service()
    user = await service.create_user("ops", PASSWORD, must_change_password=True)
    one = await service.login("ops", PASSWORD)
    two = await service.login("ops", PASSWORD)
    assert {item.id for item in await service.list_sessions(user.id)} == {one.session.id, two.session.id}

    renewed = await service.change_password(user, PASSWORD, "rotated-password-1")

    assert await service.resolve_session(one.token) is None
    assert await service.resolve_session(two.token) is None
    assert [item.id for item in await service.list_sessions(user.id)] == [renewed.session.id]
    assert renewed.user.must_change_password is False
    assert not await service.initial_password_pending()
    assert await service.revoke_session(renewed.session.id, user_id=user.id)
    assert not await service.revoke_session(renewed.session.id, user_id=user.id)


async def test_api_keys_round_trip(backend):
    first, second = backend.service(0), backend.service(1)
    created = await first.create_api_key(
        "prometheus", ["metrics:read"], created_by="user:admin", expires_in=timedelta(days=1)
    )

    resolved = await second.resolve_api_key(created.plaintext)
    assert resolved is not None
    assert resolved.scopes == frozenset({"metrics:read"})
    assert resolved.created_by == "user:admin"
    listed = await second.list_api_keys()
    assert [key.id for key in listed] == [created.key.id]
    assert listed[0].last_used_at is not None

    backend.clock.now += timedelta(days=1, seconds=1)
    assert await second.resolve_api_key(created.plaintext) is None
    backend.clock.now -= timedelta(days=1)
    assert await first.revoke_api_key(created.key.id)
    assert not await first.revoke_api_key(created.key.id)
    assert await second.resolve_api_key(created.plaintext) is None
