# tests/core/test_summary_refresher.py
"""Unit tests for SummaryRefresher."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.summary_refresher import _WINDOWS, SummaryRefresher, _ytd_start
from greenkube.models.metrics import MetricsSummaryRow, TimeseriesCachePoint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WINDOW_COUNT = len(_WINDOWS)

# Expected granularity per slug — mirrors the _WINDOWS definition.
_EXPECTED_GRANULARITIES = {slug: gran for slug, _, gran in _WINDOWS}


def _make_agg(co2=10.0, embodied=1.0, cost=0.5, energy=36000.0, pods=5, ns_count=2):
    return {
        "total_co2e_grams": co2,
        "total_embodied_co2e_grams": embodied,
        "total_cost": cost,
        "total_energy_joules": energy,
        "pod_count": pods,
        "namespace_count": ns_count,
    }


def _make_ts_rows(n=3):
    """Return n fake aggregate_timeseries dicts."""
    return [
        {
            "timestamp": f"2026-04-{14 - i:02d}T00:00:00Z",
            "co2e_grams": float(i * 10),
            "embodied_co2e_grams": float(i),
            "total_cost": float(i) * 0.1,
            "energy_joules": float(i * 3600),
        }
        for i in range(n)
    ]


def _make_refresher(metrics_repo, summary_repo, ts_repo, namespaces=None):
    return SummaryRefresher(
        metrics_repo=metrics_repo,
        summary_repo=summary_repo,
        timeseries_cache_repo=ts_repo,
        namespaces=namespaces,
    )


# ---------------------------------------------------------------------------
# _ytd_start
# ---------------------------------------------------------------------------


def test_ytd_start_returns_jan_1():
    now = datetime(2026, 4, 14, 15, 30, 0, tzinfo=timezone.utc)
    start = _ytd_start(now)
    assert start == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# SummaryRefresher.run — KPI scalar rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_upserts_all_builtin_summary_windows():
    """run() should upsert one scalar row per built-in window for the cluster."""
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())
    metrics_repo.aggregate_timeseries = AsyncMock(return_value=_make_ts_rows())
    metrics_repo.list_namespaces = AsyncMock(return_value=[])

    summary_repo = AsyncMock()
    ts_repo = AsyncMock()

    refresher = _make_refresher(metrics_repo, summary_repo, ts_repo, namespaces=[])
    await refresher.run()

    expected_slugs = {slug for slug, _, _ in _WINDOWS}
    upserted_slugs = {c.args[0].window_slug for c in summary_repo.upsert_row.call_args_list}
    assert upserted_slugs == expected_slugs


@pytest.mark.asyncio
async def test_run_upserts_timeseries_for_all_windows():
    """run() should call upsert_points once per built-in window for the cluster."""
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())
    metrics_repo.aggregate_timeseries = AsyncMock(return_value=_make_ts_rows(3))
    metrics_repo.list_namespaces = AsyncMock(return_value=[])

    summary_repo = AsyncMock()
    ts_repo = AsyncMock()

    refresher = _make_refresher(metrics_repo, summary_repo, ts_repo, namespaces=[])
    await refresher.run()

    assert ts_repo.upsert_points.call_count == _WINDOW_COUNT
    # Each call's arg should be a list of TimeseriesCachePoint
    for call in ts_repo.upsert_points.call_args_list:
        points = call.args[0]
        assert all(isinstance(p, TimeseriesCachePoint) for p in points)


@pytest.mark.asyncio
async def test_run_timeseries_points_carry_slug_and_namespace():
    """Timeseries cache points should have the correct window_slug and namespace."""
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())
    metrics_repo.aggregate_timeseries = AsyncMock(return_value=_make_ts_rows(2))

    summary_repo = AsyncMock()
    ts_repo = AsyncMock()

    refresher = _make_refresher(metrics_repo, summary_repo, ts_repo, namespaces=["prod"])
    await refresher.run()

    # Pick calls for namespace "prod"
    prod_calls = [c for c in ts_repo.upsert_points.call_args_list if c.args[0] and c.args[0][0].namespace == "prod"]
    assert len(prod_calls) == _WINDOW_COUNT
    for call in prod_calls:
        for p in call.args[0]:
            assert p.namespace == "prod"
            assert p.window_slug in {slug for slug, _, _ in _WINDOWS}


@pytest.mark.asyncio
async def test_run_upserts_per_namespace_windows():
    """run() should upsert rows for each known namespace."""
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())
    metrics_repo.aggregate_timeseries = AsyncMock(return_value=_make_ts_rows())
    metrics_repo.list_namespaces = AsyncMock(return_value=["default", "production"])

    summary_repo = AsyncMock()
    ts_repo = AsyncMock()

    refresher = _make_refresher(metrics_repo, summary_repo, ts_repo)
    await refresher.run()

    # cluster-wide + 2 namespaces
    assert summary_repo.upsert_row.call_count == _WINDOW_COUNT * 3
    assert ts_repo.upsert_points.call_count == _WINDOW_COUNT * 3


@pytest.mark.asyncio
async def test_run_tolerates_aggregate_summary_errors():
    """A failure in one aggregate_summary call should not abort other windows."""
    call_count = 0

    async def flaky_summary(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("DB timeout")
        return _make_agg()

    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = flaky_summary
    metrics_repo.aggregate_timeseries = AsyncMock(return_value=_make_ts_rows())

    summary_repo = AsyncMock()
    ts_repo = AsyncMock()

    refresher = _make_refresher(metrics_repo, summary_repo, ts_repo, namespaces=[])
    await refresher.run()

    # The failed window still writes timeseries but skips the scalar row
    assert summary_repo.upsert_row.call_count == _WINDOW_COUNT - 1


@pytest.mark.asyncio
async def test_run_row_values_match_aggregation():
    """Scalar row values should mirror what aggregate_summary returns."""
    agg = _make_agg(co2=999.9, cost=42.0, pods=7)
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=agg)
    metrics_repo.aggregate_timeseries = AsyncMock(return_value=_make_ts_rows())

    summary_repo = AsyncMock()
    ts_repo = AsyncMock()

    refresher = _make_refresher(metrics_repo, summary_repo, ts_repo, namespaces=[])
    await refresher.run()

    for c in summary_repo.upsert_row.call_args_list:
        row: MetricsSummaryRow = c.args[0]
        assert row.total_co2e_grams == pytest.approx(999.9)
        assert row.total_cost == pytest.approx(42.0)
        assert row.pod_count == 7
        assert row.updated_at is not None


@pytest.mark.asyncio
async def test_run_timeseries_point_values_match_aggregation():
    """Timeseries cache point values should mirror what aggregate_timeseries returns."""
    ts_rows = _make_ts_rows(2)
    metrics_repo = AsyncMock()
    metrics_repo.aggregate_summary = AsyncMock(return_value=_make_agg())
    metrics_repo.aggregate_timeseries = AsyncMock(return_value=ts_rows)

    summary_repo = AsyncMock()
    ts_repo = AsyncMock()

    refresher = _make_refresher(metrics_repo, summary_repo, ts_repo, namespaces=[])
    await refresher.run()

    # All calls to upsert_points should have points whose values match ts_rows
    for call in ts_repo.upsert_points.call_args_list:
        points: list[TimeseriesCachePoint] = call.args[0]
        assert len(points) == len(ts_rows)
        for p, raw in zip(points, ts_rows):
            assert p.co2e_grams == pytest.approx(raw["co2e_grams"])
            assert p.total_cost == pytest.approx(raw["total_cost"])
            assert p.bucket_ts == raw["timestamp"]


def test_windows_granularities():
    """Each window slug must map to the expected granularity for readable charts."""
    assert _EXPECTED_GRANULARITIES["24h"] == "hour"  # ≤24 bars
    assert _EXPECTED_GRANULARITIES["7d"] == "day"  # 7 bars
    assert _EXPECTED_GRANULARITIES["30d"] == "day"  # 30 bars
    assert _EXPECTED_GRANULARITIES["1y"] == "week"  # ≤53 bars
    assert _EXPECTED_GRANULARITIES["ytd"] == "month"  # ≤12 bars
