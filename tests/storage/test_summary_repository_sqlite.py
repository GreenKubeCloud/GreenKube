# tests/storage/test_summary_repository_sqlite.py
"""Tests for SQLiteSummaryRepository."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import aiosqlite
import pytest

from greenkube.models.metrics import MetricsSummaryRow
from greenkube.storage.sqlite.summary_repository import SQLiteSummaryRepository

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS metrics_summary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    window_slug TEXT    NOT NULL,
    namespace   TEXT,
    total_co2e_grams          REAL NOT NULL DEFAULT 0,
    total_embodied_co2e_grams REAL NOT NULL DEFAULT 0,
    total_cost                REAL NOT NULL DEFAULT 0,
    total_energy_joules       REAL NOT NULL DEFAULT 0,
    pod_count                 INTEGER NOT NULL DEFAULT 0,
    namespace_count           INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    UNIQUE(window_slug, namespace)
);
"""


@pytest.fixture
async def db_conn():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute(CREATE_TABLE)
        await conn.commit()
        yield conn


@pytest.fixture
def repo(db_conn):
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        yield db_conn

    db_manager.connection_scope = scope
    return SQLiteSummaryRepository(db_manager=db_manager)


def _row(
    window_slug="24h", namespace=None, co2e=100.0, embodied=10.0, cost=5.0, joules=50_000.0, pod_count=3, ns_count=2
):
    return MetricsSummaryRow(
        window_slug=window_slug,
        namespace=namespace,
        total_co2e_grams=co2e,
        total_embodied_co2e_grams=embodied,
        total_cost=cost,
        total_energy_joules=joules,
        pod_count=pod_count,
        namespace_count=ns_count,
        updated_at=datetime.now(timezone.utc),
    )


class TestUpsertRow:
    @pytest.mark.asyncio
    async def test_insert_new_row(self, repo):
        await repo.upsert_row(_row())
        assert len(await repo.get_rows()) == 1

    @pytest.mark.asyncio
    async def test_update_on_conflict(self, repo):
        """Same (window_slug, non-null namespace) → row is updated, not duplicated."""
        await repo.upsert_row(_row(window_slug="24h", namespace="prod", co2e=50.0))
        await repo.upsert_row(_row(window_slug="24h", namespace="prod", co2e=200.0))
        results = await repo.get_rows(namespace="prod")
        assert len(results) == 1
        assert results[0].total_co2e_grams == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_multiple_windows(self, repo):
        for slug in ["24h", "7d", "30d"]:
            await repo.upsert_row(_row(window_slug=slug))
        assert len(await repo.get_rows()) == 3

    @pytest.mark.asyncio
    async def test_namespace_and_cluster_independent(self, repo):
        await repo.upsert_row(_row(window_slug="24h", namespace=None))
        await repo.upsert_row(_row(window_slug="24h", namespace="prod"))
        assert len(await repo.get_rows(namespace=None)) == 1
        assert len(await repo.get_rows(namespace="prod")) == 1


class TestGetRows:
    @pytest.mark.asyncio
    async def test_empty_db(self, repo):
        assert await repo.get_rows() == []

    @pytest.mark.asyncio
    async def test_correct_fields(self, repo):
        await repo.upsert_row(_row(co2e=42.0, cost=3.14, pod_count=7))
        r = (await repo.get_rows())[0]
        assert r.window_slug == "24h"
        assert r.total_co2e_grams == pytest.approx(42.0)
        assert r.total_cost == pytest.approx(3.14)
        assert r.pod_count == 7

    @pytest.mark.asyncio
    async def test_total_co2e_all_scopes(self, repo):
        await repo.upsert_row(_row(co2e=80.0, embodied=20.0))
        r = (await repo.get_rows())[0]
        assert r.total_co2e_all_scopes == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_namespace_filter(self, repo):
        await repo.upsert_row(_row(window_slug="24h", namespace="prod"))
        await repo.upsert_row(_row(window_slug="24h", namespace="staging"))
        results = await repo.get_rows(namespace="prod")
        assert len(results) == 1
        assert results[0].namespace == "prod"

    @pytest.mark.asyncio
    async def test_ordered_by_window_slug(self, repo):
        for slug in ["7d", "24h", "30d"]:
            await repo.upsert_row(_row(window_slug=slug))
        slugs = [r.window_slug for r in await repo.get_rows()]
        assert slugs == sorted(slugs)

    @pytest.mark.asyncio
    async def test_updated_at_parsed(self, repo):
        await repo.upsert_row(_row())
        r = (await repo.get_rows())[0]
        assert isinstance(r.updated_at, datetime)
