# tests/collectors/test_pv_collector.py
"""
Tests for the PVCollector: a PersistentVolume is reported only with evidence
that it is unused (its claim is gone and nothing depends on it).

Fixtures are kubectl-shaped manifests (``tests/collectors/fixtures/deletion_evidence``).
"""

from unittest.mock import AsyncMock

import pytest

from greenkube.collectors.k8s_object_reader import STATUS_ABSENT, STATUS_UNREADABLE, StaticObjectReader
from greenkube.collectors.pv_collector import OrphanedPV, PVCollector, enrich_orphaned_pv_costs

from .deletion_fixtures import load_objects  # pyrefly: ignore[missing-import]

RETAINED = "pvc-0b7f6c1e-3d2a-4e9b-a1c8-5f4d3e2b1a09"
PREBOUND = "nfs-reserved-for-ci"
STATEFULSET_REPLICA = "pvc-5a5a5a5a-6b6b-4c7c-8d8d-9e9e9e9e9e9e"
SNAPSHOT_SOURCE = "pvc-6c6c6c6c-7d7d-4e8e-9f9f-0a0a0a0a0a0a"
LOCAL_PATH = "pvc-7e7e7e7e-8f8f-4a9a-8b0b-1c1c1c1c1c1c"
BOUND = "pvc-8f8f8f8f-9a9a-4b0b-8c1c-2d2d2d2d2d2d"


def _reader(statuses=None, mutate=None):
    objects = load_objects("persistent_volumes.json")
    if mutate:
        mutate(objects)
    return StaticObjectReader(objects, statuses=statuses)


async def _scan(reader):
    return await PVCollector(reader=reader).scan()


def _pv(objects, name):
    return next(item for item in objects["persistentvolumes"] if item["metadata"]["name"] == name)


class TestPVCollector:
    @pytest.mark.asyncio
    async def test_only_the_released_volume_without_dependants_is_a_candidate(self):
        result = await _scan(_reader())

        assert result.complete is True
        assert [c.name for c in result.candidates] == [RETAINED]
        candidate = result.candidates[0]
        assert isinstance(candidate, OrphanedPV)
        assert candidate.phase == "Released"
        assert candidate.capacity_bytes == 100 * 1024**3
        assert (candidate.claim_namespace, candidate.claim_name) == ("finance", "reports-archive")
        assert candidate.reclaim_policy == "Retain"
        assert candidate.storage_class == "gp3-retain"
        evidence = " | ".join(candidate.evidence)
        assert "no PVC named finance/reports-archive exists" in evidence
        assert "does not delete the disk" in evidence  # Retain: the cloud disk must be released manually

    @pytest.mark.asyncio
    async def test_false_positive_paths_are_skipped_with_a_reason(self):
        skipped = (await _scan(_reader())).skipped

        # Regression: a pre-bound Available volume whose claim is not created yet was reported before.
        assert skipped[PREBOUND].startswith("phase Available")
        assert skipped[BOUND].startswith("phase Bound")
        assert skipped[STATEFULSET_REPLICA] == "its claim belongs to StatefulSet db/postgres, which may scale up again"
        assert skipped[SNAPSHOT_SOURCE] == "VolumeSnapshot finance/ledger-2026-10-31 was taken from its claim"
        assert skipped[LOCAL_PATH].startswith("local or hostPath volume")

    @pytest.mark.asyncio
    async def test_snapshot_content_on_the_volume_handle_protects_it(self):
        def mutate(objects):
            objects["volumesnapshots"] = []
            objects["volumesnapshotcontents"] = [
                {
                    "kind": "VolumeSnapshotContent",
                    "metadata": {"name": "snapcontent-pre-provisioned"},
                    "spec": {"source": {"volumeHandle": "vol-0a1b2c3d4e5f60718"}},
                }
            ]

        result = await _scan(_reader(mutate=mutate))
        assert RETAINED not in [c.name for c in result.candidates]
        assert "VolumeSnapshotContent snapcontent-pre-provisioned" in result.skipped[RETAINED]

    @pytest.mark.asyncio
    async def test_recreated_claim_with_the_same_name_protects_it(self):
        def mutate(objects):
            objects["persistentvolumeclaims"].append(
                {"kind": "PersistentVolumeClaim", "metadata": {"name": "reports-archive", "namespace": "finance"}}
            )

        result = await _scan(_reader(mutate=mutate))
        assert result.candidates == []
        assert "exists again" in result.skipped[RETAINED]

    @pytest.mark.parametrize("annotation", [{"greenkube.io/ignore": "true"}, {"greenkube.io/keep": "audit"}])
    @pytest.mark.asyncio
    async def test_opt_out_annotations(self, annotation):
        def mutate(objects):
            _pv(objects, RETAINED)["metadata"]["annotations"].update(annotation)

        result = await _scan(_reader(mutate=mutate))
        assert result.candidates == []
        assert "opted out" in result.skipped[RETAINED]

    @pytest.mark.asyncio
    async def test_failed_phase_is_a_candidate(self):
        def mutate(objects):
            _pv(objects, RETAINED)["status"]["phase"] = "Failed"

        result = await _scan(_reader(mutate=mutate))
        assert [c.name for c in result.candidates] == [RETAINED]

    @pytest.mark.asyncio
    async def test_snapshot_api_not_installed_is_evidence(self):
        result = await _scan(
            _reader(statuses={"volumesnapshots": STATUS_ABSENT, "volumesnapshotcontents": STATUS_ABSENT})
        )
        # The snapshot no longer protects the ledger volume: the API is absent, so no snapshot exists.
        assert sorted(c.name for c in result.candidates) == sorted([RETAINED, SNAPSHOT_SOURCE])

    @pytest.mark.parametrize("kind", ["statefulsets", "volumesnapshots", "volumesnapshotcontents"])
    @pytest.mark.asyncio
    async def test_unreadable_evidence_means_no_candidate(self, kind):
        result = await _scan(_reader(statuses={kind: STATUS_UNREADABLE}))
        assert result.candidates == []
        assert "cannot prove it is unused" in result.skipped[RETAINED]

    @pytest.mark.parametrize("kind", ["persistentvolumes", "persistentvolumeclaims"])
    @pytest.mark.asyncio
    async def test_unreadable_volumes_or_claims_return_none(self, kind):
        assert await PVCollector(reader=_reader(statuses={kind: STATUS_UNREADABLE})).collect() is None

    @pytest.mark.asyncio
    async def test_reader_exception_returns_none(self):
        reader = AsyncMock()
        reader.list = AsyncMock(side_effect=RuntimeError("boom"))
        assert await PVCollector(reader=reader).collect() is None

    @pytest.mark.asyncio
    async def test_no_volumes_returns_empty_list(self):
        assert await PVCollector(reader=StaticObjectReader({})).collect() == []


# ---------------------------------------------------------------------------
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
