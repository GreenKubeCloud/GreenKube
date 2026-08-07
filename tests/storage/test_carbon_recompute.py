# tests/storage/test_carbon_recompute.py
"""
Tests for CombinedMetricsRepository.recompute_carbon_with_latest_intensities
(the default Python implementation, exercised through the SQLite backend).
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import aiosqlite
import pytest

from greenkube.models.metrics import CombinedMetric
from greenkube.storage.sqlite.repository import (
    SQLiteCarbonIntensityRepository,
    SQLiteCombinedMetricsRepository,
)

BASE = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db_connection():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute("""
            CREATE TABLE carbon_intensity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone TEXT NOT NULL,
                carbon_intensity REAL,
                datetime TEXT NOT NULL,
                updated_at TEXT,
                created_at TEXT,
                emission_factor_type TEXT,
                is_estimated BOOLEAN,
                estimation_method TEXT,
                UNIQUE(zone, datetime)
            );
        """)
        await conn.execute("""
            CREATE TABLE combined_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pod_name TEXT NOT NULL,
                namespace TEXT NOT NULL,
                total_cost REAL,
                co2e_grams REAL,
                pue REAL,
                grid_intensity REAL,
                joules REAL,
                cpu_request INTEGER,
                memory_request INTEGER,
                cpu_usage_millicores INTEGER,
                memory_usage_bytes INTEGER,
                network_receive_bytes REAL,
                network_transmit_bytes REAL,
                disk_read_bytes REAL,
                disk_write_bytes REAL,
                storage_request_bytes INTEGER,
                storage_usage_bytes INTEGER,
                ephemeral_storage_request_bytes INTEGER,
                ephemeral_storage_usage_bytes INTEGER,
                gpu_usage_millicores INTEGER,
                restart_count INTEGER,
                owner_kind TEXT,
                owner_name TEXT,
                period TEXT,
                "timestamp" TEXT,
                duration_seconds INTEGER,
                grid_intensity_timestamp TEXT,
                node TEXT,
                node_instance_type TEXT,
                node_zone TEXT,
                emaps_zone TEXT,
                is_estimated BOOLEAN,
                estimation_reasons TEXT,
                embodied_co2e_grams REAL,
                calculation_version TEXT,
                UNIQUE(pod_name, namespace, "timestamp")
            );
        """)
        await conn.commit()
        yield conn


@pytest.fixture
async def mock_db_manager(db_connection):
    manager = MagicMock()

    @asynccontextmanager
    async def scope():
        yield db_connection

    manager.connection_scope = scope
    return manager


@pytest.fixture
async def carbon_repo(mock_db_manager):
    return SQLiteCarbonIntensityRepository(mock_db_manager)


@pytest.fixture
async def combined_repo(mock_db_manager):
    return SQLiteCombinedMetricsRepository(mock_db_manager)


def _metric(pod: str, ns: str, ts: datetime, joules: float, pue: float, zone: str = "FR") -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace=ns,
        total_cost=0.0,
        co2e_grams=0.0,
        pue=pue,
        grid_intensity=0.0,
        joules=joules,
        timestamp=ts,
        emaps_zone=zone,
        calculation_version="test",
    )


async def _seed_history(carbon_repo, zone: str, ts: datetime, value: float, estimated: bool = True):
    await carbon_repo.save_history(
        [
            {
                "carbonIntensity": value,
                "datetime": ts.isoformat(),
                "zone": zone,
                "isEstimated": estimated,
                "estimationMethod": "wattnet_preview" if estimated else "wattnet_complete",
                "emissionFactorType": "wattnet_life-cycle_global",
            }
        ],
        zone,
    )


@pytest.mark.asyncio
async def test_recompute_updates_metrics_with_consolidated_values(carbon_repo, combined_repo):
    # Provisional history: 50 gCO2/kWh at 10:00, 55.5 at 11:00.
    await _seed_history(carbon_repo, "FR", BASE, 50.0, estimated=True)
    await _seed_history(carbon_repo, "FR", BASE + timedelta(hours=1), 55.5, estimated=True)

    # 1 kWh @ pue 1.0 at 10:30 -> should resolve to the 10:00 point (50 g/kWh).
    a = _metric("pod-a", "ns-1", BASE + timedelta(minutes=30), 3.6e6, 1.0)
    # 1 kWh @ pue 1.5 at 11:30 -> should resolve to the 11:00 point (55.5 g/kWh).
    b = _metric("pod-b", "ns-2", BASE + timedelta(minutes=90), 3.6e6, 1.5)
    # Zone without history -> must stay untouched.
    c = _metric("pod-c", "ns-3", BASE + timedelta(minutes=30), 3.6e6, 1.0, zone="US-CAL-CISO")
    await combined_repo.write_combined_metrics([a, b, c])

    updated = await combined_repo.recompute_carbon_with_latest_intensities(
        carbon_repo, BASE - timedelta(days=1), BASE + timedelta(days=1)
    )
    assert updated == 2

    rows = await combined_repo.read_combined_metrics(BASE - timedelta(days=1), BASE + timedelta(days=1))
    by_pod = {m.pod_name: m for m in rows}
    assert by_pod["pod-a"].grid_intensity == 50.0
    assert by_pod["pod-a"].co2e_grams == pytest.approx(50.0)
    assert by_pod["pod-b"].grid_intensity == 55.5
    assert by_pod["pod-b"].co2e_grams == pytest.approx(1.5 * 55.5)
    assert by_pod["pod-c"].grid_intensity == 0.0
    assert by_pod["pod-c"].co2e_grams == 0.0

    # Provider consolidates: the 10:00 point is revised to 48.2 gCO2/kWh.
    await _seed_history(carbon_repo, "FR", BASE, 48.2, estimated=False)

    updated = await combined_repo.recompute_carbon_with_latest_intensities(
        carbon_repo, BASE - timedelta(days=1), BASE + timedelta(days=1)
    )
    assert updated == 1

    rows = await combined_repo.read_combined_metrics(BASE - timedelta(days=1), BASE + timedelta(days=1))
    by_pod = {m.pod_name: m for m in rows}
    assert by_pod["pod-a"].grid_intensity == 48.2
    assert by_pod["pod-a"].co2e_grams == pytest.approx(48.2)
    # Unchanged row is not rewritten.
    assert by_pod["pod-b"].grid_intensity == 55.5


@pytest.mark.asyncio
async def test_recompute_respects_window(carbon_repo, combined_repo):
    # History exists for both an in-window and an out-of-window timestamp.
    await _seed_history(carbon_repo, "FR", BASE, 50.0)
    await _seed_history(carbon_repo, "FR", BASE - timedelta(hours=30), 99.0)

    in_window = _metric("pod-a", "ns-1", BASE + timedelta(minutes=30), 3.6e6, 1.0)
    out_window = _metric("pod-b", "ns-2", BASE - timedelta(hours=29), 3.6e6, 1.0)
    await combined_repo.write_combined_metrics([in_window, out_window])

    updated = await combined_repo.recompute_carbon_with_latest_intensities(
        carbon_repo, BASE - timedelta(hours=24), BASE + timedelta(hours=1)
    )
    assert updated == 1

    rows = await combined_repo.read_combined_metrics(BASE - timedelta(hours=48), BASE + timedelta(hours=1))
    by_pod = {m.pod_name: m for m in rows}
    assert by_pod["pod-a"].grid_intensity == 50.0
    assert by_pod["pod-b"].grid_intensity == 0.0  # untouched


@pytest.mark.asyncio
async def test_recompute_namespace_filter(carbon_repo, combined_repo):
    await _seed_history(carbon_repo, "FR", BASE, 50.0)
    a = _metric("pod-a", "target-ns", BASE + timedelta(minutes=30), 3.6e6, 1.0)
    b = _metric("pod-b", "other-ns", BASE + timedelta(minutes=30), 3.6e6, 1.0)
    await combined_repo.write_combined_metrics([a, b])

    updated = await combined_repo.recompute_carbon_with_latest_intensities(
        carbon_repo, BASE - timedelta(days=1), BASE + timedelta(days=1), namespace="target-ns"
    )
    assert updated == 1

    rows = await combined_repo.read_combined_metrics(BASE - timedelta(days=1), BASE + timedelta(days=1))
    by_pod = {m.pod_name: m for m in rows}
    assert by_pod["pod-a"].grid_intensity == 50.0
    assert by_pod["pod-b"].grid_intensity == 0.0


@pytest.mark.asyncio
async def test_recompute_no_changes_returns_zero(carbon_repo, combined_repo):
    await _seed_history(carbon_repo, "FR", BASE, 50.0)
    a = _metric("pod-a", "ns-1", BASE + timedelta(minutes=30), 3.6e6, 1.0)
    await combined_repo.write_combined_metrics([a])

    assert (
        await combined_repo.recompute_carbon_with_latest_intensities(
            carbon_repo, BASE - timedelta(days=1), BASE + timedelta(days=1)
        )
        == 1
    )
    # Second pass: nothing changed.
    assert (
        await combined_repo.recompute_carbon_with_latest_intensities(
            carbon_repo, BASE - timedelta(days=1), BASE + timedelta(days=1)
        )
        == 0
    )
