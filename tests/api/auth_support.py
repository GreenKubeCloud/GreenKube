"""Shared helpers for the authentication API tests (roadmap F14, F15).

Each test gets a real SQLite database file, an app whose ``auth_service`` uses
it, and a controllable clock. Accounts are created before the TestClient
starts, on a separate connection, because the app connects lazily inside the
TestClient event loop.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import Depends
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_authenticated_actor
from greenkube.auth.service import AuthService
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.storage.sqlite.auth_repository import SQLiteAuthRepository

ADMIN_PASSWORD = "initial-admin-password"
VIEWER_PASSWORD = "viewer-password-123"
ORIGIN = "http://testserver"


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class AuthHarness:
    client: TestClient
    service: AuthService
    clock: Clock
    config: Config

    def login(self, username: str = "admin", password: str = ADMIN_PASSWORD, *, remember: bool = False, **kwargs):
        headers = {"Origin": ORIGIN, **kwargs.pop("headers", {})}
        return self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password, "remember": remember},
            headers=headers,
            **kwargs,
        )

    def csrf(self) -> dict[str, str]:
        me = self.client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        return {"Origin": ORIGIN, "X-CSRF-Token": me.json()["csrf_token"]}


def _make_config(db_path: str, monkeypatch, **env: str) -> Config:
    from greenkube.core.config import config

    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", db_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # The autouse conftest fixture reloads the singleton again for the next test.
    config.reload()
    return config


async def _seed(cfg: Config, users: list[tuple[str, str, str, bool]]) -> None:
    manager = DatabaseManager(cfg)
    await manager.connect()
    try:
        service = AuthService(SQLiteAuthRepository(manager))
        for username, password, role, must_change in users:
            await service.create_user(username, password, role=role, must_change_password=must_change)
    finally:
        await manager.close()


@contextmanager
def auth_harness(
    tmp_path,
    monkeypatch,
    *,
    users: list[tuple[str, str, str, bool]] | None = None,
    client_address: tuple[str, int] = ("testclient", 50000),
    base_url: str = "http://testserver",
    asgi_wrapper=None,
    **env: str,
):
    """Yield an :class:`AuthHarness` for an app with sessions and a seeded account store."""
    env.setdefault("API_AUTH_MODE", "session")
    env.setdefault("API_RATE_LIMIT", "1000/minute")
    cfg = _make_config(str(tmp_path / "auth.sqlite"), monkeypatch, **env)
    seeded = (
        users
        if users is not None
        else [("admin", ADMIN_PASSWORD, "admin", False), ("viewer", VIEWER_PASSWORD, "viewer", False)]
    )
    asyncio.run(_seed(cfg, seeded))
    clock = Clock()
    app = create_app()

    @app.get("/api/v1/test-support/actor")
    async def _actor(actor: str = Depends(get_authenticated_actor)):
        return {"actor": actor}

    manager = DatabaseManager(cfg)
    service = AuthService(SQLiteAuthRepository(manager), clock=clock)
    app.state.auth_service = service
    served = asgi_wrapper(app) if asgi_wrapper is not None else app
    with TestClient(served, client=client_address, base_url=base_url) as client:
        try:
            yield AuthHarness(client=client, service=service, clock=clock, config=cfg)
        finally:
            client.portal.call(manager.close)
