"""Resource binding storage (migration 0087, roadmap H0d/H6) on SQLite and PostgreSQL."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.gitops.contracts import ControllerKind, ResourceTarget
from greenkube.gitops.discovery.inventory import ResourceBinding
from greenkube.storage.resource_binding_repository import ResourceBindingRepository
from tests.gitops.planning.test_raw_manifest import _source  # pyrefly: ignore[missing-import]

LB = ResourceTarget(api_version="v1", kind="Service", namespace="shop", name="old-lb")
PV = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-1")
ARGO = (ControllerKind.ARGOCD, "argocd", "shop")
LOCATION = _source()


def _configure(backend: str, tmp_path, monkeypatch) -> tuple[str | None, str | None]:
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_resource_bindings_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
        return dsn, schema
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "resource-bindings.sqlite"))
    return None, None


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    dsn, schema = _configure(request.param, tmp_path, monkeypatch)
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _binding(ref: ResourceTarget, *, prune: bool | None = True) -> ResourceBinding:
    return ResourceBinding(ref=ref, status="resolved", controllers=(ARGO,), location=LOCATION, prune=prune)


async def test_a_refresh_replaces_the_cluster_bindings(db) -> None:
    repo = ResourceBindingRepository(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert await repo.replace_cluster("prod", [_binding(LB), _binding(PV, prune=None)], observed_at=now) == 2
    await repo.replace_cluster("staging", [_binding(LB)], observed_at=now)

    stored = await repo.get("prod", PV)
    assert stored is not None
    assert stored.binding == _binding(PV, prune=None)
    assert stored.observed_at == now

    later = now + timedelta(minutes=5)
    await repo.replace_cluster("prod", [_binding(PV, prune=False)], observed_at=later)
    assert await repo.get("prod", LB) is None
    refreshed = await repo.get("prod", PV)
    assert refreshed is not None and refreshed.binding.prune is False and refreshed.observed_at == later
    assert [item.binding.ref for item in await repo.list_cluster("staging")] == [LB]


async def test_lookup_is_version_insensitive_and_answers_with_the_asked_ref(db) -> None:
    repo = ResourceBindingRepository(db)
    hpa_v2 = ResourceTarget(api_version="autoscaling/v2", kind="HorizontalPodAutoscaler", namespace="shop", name="api")
    await repo.replace_cluster("prod", [_binding(hpa_v2)], observed_at=datetime.now(timezone.utc))

    asked = hpa_v2.model_copy(update={"api_version": "autoscaling/v1"})
    stored = await repo.get("prod", asked)
    assert stored is not None and stored.binding.ref == asked
    assert await repo.get("prod", hpa_v2.model_copy(update={"name": "other"})) is None
