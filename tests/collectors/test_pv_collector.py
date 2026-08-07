# tests/collectors/test_pv_collector.py
"""
Tests for the PVCollector that detects orphaned PersistentVolumes.
TDD: Tests written before implementation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.pv_collector import OrphanedPV, PVCollector, enrich_orphaned_pv_costs


@pytest.fixture
def pv_collector():
    """Returns a PVCollector instance."""
    return PVCollector()


def _make_pv(name, phase="Bound", capacity="100Gi", claim_ref=None, reclaim_policy="Delete"):
    """Helper to create a mock PersistentVolume object."""
    pv = MagicMock()
    pv.metadata = MagicMock()
    pv.metadata.name = name
    pv.spec = MagicMock()
    pv.spec.capacity = {"storage": capacity}
    pv.spec.claim_ref = claim_ref
    pv.spec.persistent_volume_reclaim_policy = reclaim_policy
    pv.spec.storage_class_name = "standard"
    pv.status = MagicMock()
    pv.status.phase = phase
    return pv


def _make_pvc(namespace, name):
    """Helper to create a mock PersistentVolumeClaim object."""
    pvc = MagicMock()
    pvc.metadata = MagicMock()
    pvc.metadata.namespace = namespace
    pvc.metadata.name = name
    return pvc


def _claim_ref(namespace, name):
    """Helper to create a mock claimRef object."""
    ref = MagicMock()
    ref.namespace = namespace
    ref.name = name
    return ref


def _mock_api(pvs, pvcs):
    """Creates a mock CoreV1Api returning the given PV and PVC lists."""
    api = AsyncMock()
    pv_list = MagicMock()
    pv_list.items = pvs
    pvc_list = MagicMock()
    pvc_list.items = pvcs
    api.list_persistent_volume = AsyncMock(return_value=pv_list)
    api.list_persistent_volume_claim_for_all_namespaces = AsyncMock(return_value=pvc_list)
    return api


class TestPVCollector:
    """Tests for PVCollector.collect()."""

    @pytest.mark.asyncio
    async def test_collect_detects_released_volume(self, pv_collector):
        """A PV in Released phase with a missing claim should be flagged."""
        pv = _make_pv(
            "pvc-dead",
            phase="Released",
            capacity="100Gi",
            claim_ref=_claim_ref("default", "gone-claim"),
            reclaim_policy="Retain",
        )
        with patch(
            "greenkube.collectors.pv_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([pv], []),
        ):
            orphaned = await pv_collector.collect()

        assert len(orphaned) == 1
        result = orphaned[0]
        assert result.name == "pvc-dead"
        assert result.phase == "Released"
        assert result.capacity_bytes == 100 * 1024**3
        assert result.claim_namespace == "default"
        assert result.claim_name == "gone-claim"
        assert result.reclaim_policy == "Retain"

    @pytest.mark.asyncio
    async def test_collect_detects_bound_volume_with_missing_claim(self, pv_collector):
        """A PV whose claimRef points to a non-existent PVC should be flagged."""
        pv = _make_pv(
            "pvc-stale",
            phase="Bound",
            capacity="50Gi",
            claim_ref=_claim_ref("prod", "deleted-pvc"),
        )
        with patch(
            "greenkube.collectors.pv_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([pv], [_make_pvc("prod", "other-pvc")]),
        ):
            orphaned = await pv_collector.collect()

        assert len(orphaned) == 1
        assert orphaned[0].name == "pvc-stale"
        assert orphaned[0].capacity_bytes == 50 * 1024**3

    @pytest.mark.asyncio
    async def test_collect_skips_volume_with_live_claim(self, pv_collector):
        """A bound PV whose PVC still exists should NOT be flagged."""
        pv = _make_pv(
            "pvc-alive",
            phase="Bound",
            capacity="10Gi",
            claim_ref=_claim_ref("default", "live-claim"),
        )
        with patch(
            "greenkube.collectors.pv_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([pv], [_make_pvc("default", "live-claim")]),
        ):
            orphaned = await pv_collector.collect()

        assert orphaned == []

    @pytest.mark.asyncio
    async def test_collect_skips_available_volume_without_claim(self, pv_collector):
        """An Available PV without claimRef (static provisioning) should NOT be flagged."""
        pv = _make_pv("static-volume", phase="Available", capacity="500Gi", claim_ref=None)
        with patch(
            "greenkube.collectors.pv_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([pv], []),
        ):
            orphaned = await pv_collector.collect()

        assert orphaned == []

    @pytest.mark.asyncio
    async def test_collect_returns_empty_when_no_volumes(self, pv_collector):
        """Should return an empty list if no PVs exist."""
        with patch(
            "greenkube.collectors.pv_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([], []),
        ):
            orphaned = await pv_collector.collect()

        assert orphaned == []

    @pytest.mark.asyncio
    async def test_collect_returns_empty_when_api_unavailable(self, pv_collector):
        """Should return an empty list if the K8s API is unavailable."""
        with patch(
            "greenkube.collectors.pv_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=None,
        ):
            orphaned = await pv_collector.collect()

        assert orphaned == []

    @pytest.mark.asyncio
    async def test_collect_handles_exception_gracefully(self, pv_collector):
        """Should return an empty list on exception without crashing."""
        mock_api = AsyncMock()
        mock_api.list_persistent_volume = AsyncMock(side_effect=Exception("API timeout"))

        with patch(
            "greenkube.collectors.pv_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=mock_api,
        ):
            orphaned = await pv_collector.collect()

        assert orphaned == []


# ---------------------------------------------------------------------------
# enrich_orphaned_pv_costs
# ---------------------------------------------------------------------------


class TestEnrichOrphanedPvCosts:
    """Tests for the OpenCost cost enrichment of orphaned PVs."""

    @pytest.mark.asyncio
    async def test_annualizes_opencost_window_costs(self):
        """Window costs should be annualized against the observation window."""
        from unittest.mock import AsyncMock

        from greenkube.collectors.opencost_collector import OpenCostCollector

        volumes = [
            OrphanedPV(name="pvc-aaa", phase="Released", capacity_bytes=10 * 1024**3),
            OrphanedPV(name="pvc-bbb", phase="Released", capacity_bytes=10 * 1024**3),
        ]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_pv_costs = AsyncMock(return_value={"pvc-aaa": 2.5})

        result = await enrich_orphaned_pv_costs(volumes, window_days=7, opencost=mock_opencost)

        # 2.5 USD over 7 days → 2.5 * 365/7 per year
        assert result[0].annual_cost == pytest.approx(2.5 * 365 / 7)
        assert result[1].annual_cost is None
        mock_opencost.collect_pv_costs.assert_awaited_once_with(["pvc-aaa", "pvc-bbb"], window_days=7)

    @pytest.mark.asyncio
    async def test_keeps_volumes_unchanged_without_opencost_data(self):
        """Volumes without OpenCost data should keep annual_cost=None."""
        from unittest.mock import AsyncMock

        from greenkube.collectors.opencost_collector import OpenCostCollector

        volumes = [OrphanedPV(name="pvc-aaa", phase="Released", capacity_bytes=10 * 1024**3)]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_pv_costs = AsyncMock(return_value={})

        result = await enrich_orphaned_pv_costs(volumes, window_days=7, opencost=mock_opencost)

        assert result[0].annual_cost is None

    @pytest.mark.asyncio
    async def test_handles_opencost_failure_gracefully(self):
        """An OpenCost failure should leave volumes untouched without raising."""
        from unittest.mock import AsyncMock

        from greenkube.collectors.opencost_collector import OpenCostCollector

        volumes = [OrphanedPV(name="pvc-aaa", phase="Released", capacity_bytes=10 * 1024**3)]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_pv_costs = AsyncMock(side_effect=RuntimeError("boom"))

        result = await enrich_orphaned_pv_costs(volumes, window_days=7, opencost=mock_opencost)

        assert result is volumes
        assert result[0].annual_cost is None

    @pytest.mark.asyncio
    async def test_skips_opencost_for_empty_volumes(self):
        """No volumes means no OpenCost query."""
        from unittest.mock import AsyncMock

        from greenkube.collectors.opencost_collector import OpenCostCollector

        mock_opencost = AsyncMock(spec=OpenCostCollector)

        result = await enrich_orphaned_pv_costs([], opencost=mock_opencost)

        assert result == []
        mock_opencost.collect_pv_costs.assert_not_awaited()
