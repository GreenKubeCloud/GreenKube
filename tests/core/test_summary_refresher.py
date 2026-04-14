# tests/core/test_summary_refresher.py
"""Unit tests for SummaryRefresher."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.summary_refresher import _WINDOWS, SummaryRefresher, _ytd_start
from greenkube.models.metrics import MetricsSummaryRow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agg(co2=10.0, embodied=1.0, cost=0.5, energy=36000.0, pods=5, ns_count=2):
    return {
        "total_co2e_grams": co2,
        "total_embodied_co2e_grams": embodied,
        "total_cost": cost,
        "total_energy_joules": energy,
        "pod_count": pods,
        "namespace_count": ns_count,
    }


# ---------------------------------------------------------------------------
# _ytd_start
# ---------------------------------------------------------------------------


def test_ytd_start_returns_jan_1():
    now = datetime(2026, 4, 14, 15, 30, 0, tzinfo=timezone.utc)
    start = _ytd_start(now)
    assert start == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# SummaryRefresher.run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_upserts_all_builtin_windows():
    """run() should upsert one row per built-in window for the cluster."""
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())
    metrics_repo.list_namespaces = AsyncMock(return_value=[])

    summary_repo = AsyncMock()
    summary_repo.upsert_row = AsyncMock()

    refresher = SummaryRefresher(
        metrics_repo=metrics_repo,
        summary_repo=summary_repo,
        namespaces=[],
    )
    count = await refresher.run()

    expected_slugs = {slug for slug, _ in _WINDOWS}
    upserted_slugs = {call.args[0].window_slug for call in summary_repo.upsert_row.call_args_list}
    assert upserted_slugs == expected_slugs
    assert count == len(_WINDOWS)


@pytest.mark.asyncio
async def test_run_upserts_per_namespace_windows():
    """run() should also upsert rows for each known namespace."""
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())
    metrics_repo.list_namespaces = AsyncMock(return_value=["default", "production"])

    summary_repo = AsyncMock()
    summary_repo.upsert_row = AsyncMock()

    refresher = SummaryRefresher(
        metrics_repo=metrics_repo,
        summary_repo=summary_repo,
    )
    count = await refresher.run()

    # cluster-wide + 2 namespaces, each with len(_WINDOWS) rows
    assert count == len(_WINDOWS) * 3


@pytest.mark.asyncio
async def test_run_sets_namespace_on_row():
    """Rows for a namespace should have the namespace field set."""
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())

    summary_repo = AsyncMock()
    summary_repo.upsert_row = AsyncMock()

    refresher = SummaryRefresher(
        metrics_repo=metrics_repo,
        summary_repo=summary_repo,
        namespaces=["staging"],
    )
    await refresher.run()

    ns_rows = [c.args[0] for c in summary_repo.upsert_row.call_args_list if c.args[0].namespace == "staging"]
    assert len(ns_rows) == len(_WINDOWS)


@pytest.mark.asyncio
async def test_run_tolerates_aggregate_errors():
    """A failure in one window should not abort other windows."""
    call_count = 0

    async def flaky_aggregate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("DB timeout")
        return _make_agg()

    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = flaky_aggregate

    summary_repo = AsyncMock()
    summary_repo.upsert_row = AsyncMock()

    refresher = SummaryRefresher(
        metrics_repo=metrics_repo,
        summary_repo=summary_repo,
        namespaces=[],
    )
    count = await refresher.run()

    # All windows except the failed one should be upserted
    assert count == len(_WINDOWS) - 1


@pytest.mark.asyncio
async def test_run_row_values_match_aggregation():
    """Row values should mirror what aggregate_summary returns."""
    agg = _make_agg(co2=999.9, cost=42.0, pods=7)
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=agg)

    summary_repo = AsyncMock()
    summary_repo.upsert_row = AsyncMock()

    refresher = SummaryRefresher(
        metrics_repo=metrics_repo,
        summary_repo=summary_repo,
        namespaces=[],
    )
    await refresher.run()

    for c in summary_repo.upsert_row.call_args_list:
        row: MetricsSummaryRow = c.args[0]
        assert row.total_co2e_grams == pytest.approx(999.9)
        assert row.total_cost == pytest.approx(42.0)
        assert row.pod_count == 7
        assert row.updated_at is not None
