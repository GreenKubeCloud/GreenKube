"""``greenkube auth reset-password`` (N11 follow-up): recover a local account once the initial password is gone."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from greenkube.auth.service import AuthError, AuthService
from greenkube.cli.main import app
from greenkube.core.config import config
from greenkube.core.db import DatabaseManager
from greenkube.storage.sqlite.auth_repository import SQLiteAuthRepository


async def _with_service(callback):
    manager = DatabaseManager(config)
    await manager.connect()
    try:
        return await callback(AuthService(SQLiteAuthRepository(manager)))
    finally:
        await manager.close()


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "auth.sqlite"))
    config.reload()


def test_reset_password_prints_a_new_password_and_signs_out_sessions(sqlite_db):
    async def seed(service):
        await service.create_user("admin", "forgotten-password", role="admin")
        issued = await service.login("admin", "forgotten-password")
        return issued.token

    token = asyncio.run(_with_service(seed))
    result = CliRunner().invoke(app, ["auth", "reset-password", "admin"])

    assert result.exit_code == 0, result.output
    password = result.stdout.strip().splitlines()[-1]
    assert len(password) >= 20

    async def check(service):
        assert await service.resolve_session(token) is None
        with pytest.raises(AuthError):
            await service.login("admin", "forgotten-password")
        issued = await service.login("admin", password)
        assert issued.user.must_change_password is True

    asyncio.run(_with_service(check))


def test_reset_password_refuses_unknown_and_sso_accounts(sqlite_db):
    async def seed(service):
        await service.create_user("admin", "some-password-123", role="admin")
        await service.sso_user("oidc", "sub-1", "alice", {"x"})

    asyncio.run(_with_service(seed))
    runner = CliRunner()
    unknown = runner.invoke(app, ["auth", "reset-password", "nobody"])
    assert unknown.exit_code == 1 and "No account" in unknown.output
    sso = runner.invoke(app, ["auth", "reset-password", "alice"])
    assert sso.exit_code == 1 and "single sign-on" in sso.output
