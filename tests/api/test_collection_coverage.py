"""GET /api/v1/collection/coverage and the collection fields of /api/v1/health (COL-04)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.api.routers import collection as collection_router
from greenkube.core.collection import METRICS_WATERMARK_SOURCE, collection_cluster_key
from greenkube.core.config import get_config
from greenkube.storage.base_collection_watermark_repository import CollectionWatermark
from greenkube.storage.base_collection_window_repository import (
    REASON_BEYOND_BACKFILL,
    REASON_NO_SCRAPE_DATA,
    STATUS_COLLECTED,
    STATUS_FAILED,
    STATUS_GAP,
    CollectionWindowRecord,
)

STEP = timedelta(minutes=5)
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


class _Windows:
    def __init__(self, records):
        self.records = records

    async def list_overlapping(self, cluster_uid, source, start, end):
        return [r for r in self.records if r.window_start < end and r.window_end > start]

    async def last_hole(self, cluster_uid, source):
        holes = [r for r in self.records if r.status in (STATUS_GAP, STATUS_FAILED)]
        return max(holes, key=lambda r: r.window_end) if holes else None


class _Watermarks:
    def __init__(self, end):
        self.end = end

    async def get(self, cluster_uid, source):
        return CollectionWatermark(cluster_uid, source, self.end, self.end)


def _record(start, status, steps=1, reason=None):
    return CollectionWindowRecord(
        cluster_uid=collection_cluster_key(get_config()),
        source=METRICS_WATERMARK_SOURCE,
        window_start=start,
        window_end=start + steps * STEP,
        status=status,
        expected_steps=steps,
        steps_with_data=1 if status == STATUS_COLLECTED else 0,
        reason=reason,
    )


@pytest.fixture
def records():
    # One hour: 6 steps abandoned, 5 failed, 1 collected.
    return [
        _record(BASE, STATUS_GAP, steps=6, reason=REASON_BEYOND_BACKFILL),
        *[_record(BASE + k * STEP, STATUS_FAILED, reason=REASON_NO_SCRAPE_DATA) for k in range(6, 11)],
        _record(BASE + 11 * STEP, STATUS_COLLECTED),
    ]


@pytest.fixture
def wired(client, records, monkeypatch):
    windows = _Windows(records)

    async def _windows():
        return windows

    async def _watermarks():
        return _Watermarks(BASE + 12 * STEP)

    client.app.dependency_overrides[collection_router.get_collection_window_repository] = _windows
    monkeypatch.setattr(collection_router, "get_collection_window_repository", _windows)
    monkeypatch.setattr(collection_router, "get_collection_watermark_repository", _watermarks)
    monkeypatch.setattr(get_config(), "PROMETHEUS_QUERY_RANGE_STEP", "5m", raising=False)
    yield client
    client.app.dependency_overrides.pop(collection_router.get_collection_window_repository, None)


def test_coverage_reports_the_holes_of_the_period(wired):
    response = wired.get(
        "/api/v1/collection/coverage",
        params={"start": BASE.isoformat(), "end": (BASE + timedelta(hours=1)).isoformat()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expected_steps"] == 12
    assert (body["collected_steps"], body["failed_steps"], body["gap_steps"]) == (1, 5, 6)
    assert body["coverage_ratio"] == pytest.approx(1 / 12)
    assert body["complete"] is False
    assert [(h["status"], h["reason"]) for h in body["holes"]] == [
        (STATUS_GAP, REASON_BEYOND_BACKFILL),
        (STATUS_FAILED, REASON_NO_SCRAPE_DATA),
    ]


def test_coverage_rejects_an_inverted_period(wired):
    response = wired.get(
        "/api/v1/collection/coverage",
        params={"start": BASE.isoformat(), "end": (BASE - timedelta(hours=1)).isoformat()},
    )

    assert response.status_code == 400


def test_health_shows_the_watermark_lag_and_the_last_gap(wired):
    """COL-04 acceptance c)."""
    body = wired.get("/api/v1/health").json()

    assert body["status"] == "ok"
    collection = body["collection"]
    assert collection["watermark"].startswith("2026-11-07T11:00:00")
    assert collection["lag_seconds"] > 0
    assert collection["last_gap"]["status"] == STATUS_FAILED
    assert collection["last_gap"]["reason"] == REASON_NO_SCRAPE_DATA
