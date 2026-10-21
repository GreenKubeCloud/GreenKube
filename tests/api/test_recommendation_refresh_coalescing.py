"""Concurrent recommendation refreshes share one active scan."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from greenkube.api.routers import recommendations as recommendations_router


@pytest.mark.asyncio
async def test_concurrent_refreshes_coalesce_into_one_engine_run(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def refresh(self, repo, node_repo, reco_repo, namespace=None):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr("greenkube.core.optimization.engine.OptimizationEngine.refresh", refresh)
    monkeypatch.setattr(recommendations_router, "update_recommendation_metrics", lambda records: None)

    arguments = ("prod", cast(Any, object()), cast(Any, object()), cast(Any, object()))
    first = asyncio.create_task(recommendations_router._generate_and_persist_recommendations(*arguments))
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(recommendations_router._generate_and_persist_recommendations(*arguments))
    await asyncio.sleep(0.05)

    assert calls == 1
    release.set()
    assert await asyncio.gather(first, second) == [[], []]
    assert recommendations_router._ACTIVE_REFRESH_TASKS == {}
