"""Apply detection records changes and baseline snapshots (C1)."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from greenkube.core.db import db_manager
from greenkube.core.optimization.applied_detector import AppliedDetector, WorkloadSnapshot
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.verifier import RecommendationVerifier
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.change_repository import ChangeRepository
from greenkube.storage.measurement_repository import SavingsMeasurementRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.verify.model import MeasurementState, TimeWindow
from greenkube.verify.service import ChangeMeasurementService
from tests.core.optimization.verification_fixtures import WindowedMetricsRepo  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import Settings, hour_now, workload_rows  # pyrefly: ignore[missing-import]

CONFIG: Any = Settings()


@pytest.fixture
async def stores():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager), ChangeRepository(db_manager)
    await db_manager.close()


class FakeReader:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.reads = 0

    async def read(self):
        self.reads += 1
        return self.snapshots


def _records():
    common = dict(
        pod_name="api",
        namespace="prod",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        container_name="api",
        status=RecommendationStatus.ACTIVE,
        description="oversized",
    )
    return [
        RecommendationRecord(
            type=RecommendationType.RIGHTSIZING_CPU,
            current_cpu_request_millicores=1000,
            recommended_cpu_request_millicores=300,
            potential_savings_cost=500.0,
            **common,
        ),
        RecommendationRecord(
            type=RecommendationType.RIGHTSIZING_MEMORY,
            current_memory_request_bytes=2 * 1024**3,
            recommended_memory_request_bytes=1024**3,
            potential_savings_cost=200.0,
            **common,
        ),
    ]


def _snapshot(generation=2, digest="digest-after", cpu=300, memory=1024**3):
    return WorkloadSnapshot(
        namespace="prod",
        kind="Deployment",
        name="api",
        cpu_request_millicores=cpu,
        memory_request_bytes=memory,
        container_requests={"api": (cpu, memory)},
        uid="uid-api",
        generation=generation,
        observed_generation=generation,
        spec_digest=digest,
        load_metric="sum(rate(http_requests_total[5m]))",
    )


def _service(reco_repo, change_repo, metrics):
    lifecycle = RecommendationLifecycle(reco_repo)
    settings: Any = Settings()
    verifier = RecommendationVerifier(
        lifecycle, metrics, config=settings, measurement_repo=SavingsMeasurementRepository(db_manager)
    )
    service = ChangeMeasurementService(lifecycle, metrics, change_repo, verifier=verifier, config=settings)
    return lifecycle, service


async def test_one_change_per_rollout_with_a_frozen_baseline(stores):
    reco_repo, change_repo = stores
    await reco_repo.save_recommendations(_records())
    pre_window = TimeWindow(start=hour_now() - timedelta(days=10), end=hour_now())
    metrics = WindowedMetricsRepo(pod_rows=workload_rows(pre_window))
    lifecycle, service = _service(reco_repo, change_repo, metrics)

    # A first pass sees the workload before the change: its revision is observed.
    before = FakeReader(
        {("prod", "Deployment", "api"): _snapshot(generation=1, digest="digest-before", cpu=1000, memory=2 * 1024**3)}
    )
    detector = AppliedDetector(lifecycle, reader=before, config=CONFIG, change_recorder=service)
    assert await detector.detect() == []

    after = FakeReader({("prod", "Deployment", "api"): _snapshot()})
    detector = AppliedDetector(lifecycle, reader=after, config=CONFIG, change_recorder=service)
    applied = await detector.detect()
    assert len(applied) == 2

    change_ids = set()
    for applied_record in applied:
        assert applied_record.baseline is not None
        change_ids.add(applied_record.baseline.model_dump()["change_id"])
    assert len(change_ids) == 1
    change = await change_repo.get_change(change_ids.pop())
    assert change is not None
    assert change.recommendation_ids == sorted(record.id for record in applied if record.id is not None)
    assert change.containers == ["api"]
    assert (change.generation_before, change.generation_after) == (1, 2)
    assert (change.spec_digest_before, change.spec_digest_after) == ("digest-before", "digest-after")
    assert change.details["spec_before_source"] == "observed"
    assert change.load_metric == "sum(rate(http_requests_total[5m]))"
    assert change.rollout_completed_at >= change.baseline_window_end

    status = await change_repo.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.APPLIED
    snapshot = await change_repo.get_snapshot_for_change(change.change_id)
    assert snapshot is not None and snapshot.verify_digest()
    assert snapshot.window.duration == timedelta(days=7)
    assert snapshot.window.end == change.baseline_window_end
    assert sum(1 for bucket in snapshot.hours if bucket.cost is not None) >= 160
    assert "cpu_usage" in snapshot.load_series

    # Re-detection never duplicates the change.
    again = await service.change_id_for(None, _snapshot(), change.rollout_completed_at)
    assert again == change.change_id


async def test_detector_reads_workloads_for_open_changes_without_candidates(stores):
    reco_repo, change_repo = stores
    lifecycle, service = _service(reco_repo, change_repo, WindowedMetricsRepo())
    reader = FakeReader({("prod", "Deployment", "api"): _snapshot()})
    detector = AppliedDetector(lifecycle, reader=reader, config=CONFIG, change_recorder=service)
    assert await detector.detect() == []
    assert reader.reads == 0

    await reco_repo.save_recommendations(_records()[:1])
    records = await reco_repo.get_recommendations_by_statuses(["active"])
    await lifecycle.apply(records[0].id, actual_cpu=300, change_id="c-open", applied_at=hour_now())
    applied = await reco_repo.get_recommendation_by_id(records[0].id)
    await service.record_change("c-open", [applied], snapshot=_snapshot())
    assert await detector.detect() == []
    assert reader.reads == 1
    observations = await change_repo.spec_observations("prod", "Deployment", "api")
    assert [item.spec_digest for item in observations] == ["digest-after"]
    # An unchanged revision is not observed twice.
    await detector.detect()
    assert len(await change_repo.spec_observations("prod", "Deployment", "api")) == 1


async def test_disabled_change_measurement_keeps_the_interim_verifier(stores):
    reco_repo, change_repo = stores
    await reco_repo.save_recommendations(_records()[:1])
    lifecycle = RecommendationLifecycle(reco_repo)
    settings: Any = Settings(CHANGE_MEASUREMENT_ENABLED=False)
    service = ChangeMeasurementService(lifecycle, WindowedMetricsRepo(), change_repo, config=settings)
    reader = FakeReader({("prod", "Deployment", "api"): _snapshot()})
    detector = AppliedDetector(lifecycle, reader=reader, config=settings, change_recorder=service)
    applied = await detector.detect()
    assert len(applied) == 1
    assert applied[0].baseline is not None
    assert applied[0].baseline.model_dump().get("change_id") is None
    assert await change_repo.list_statuses(["applied"]) == []
    assert await service.run_due() == []


async def test_a_failed_hpa_listing_is_not_recorded_as_an_hpa_change(stores):
    reco_repo, change_repo = stores
    _, service = _service(reco_repo, change_repo, WindowedMetricsRepo())
    key = ("prod", "Deployment", "api")
    now = hour_now()
    with_hpa = WorkloadSnapshot(namespace="prod", kind="Deployment", name="api", uid="uid-api", generation=2)
    with_hpa.spec_digest, with_hpa.hpa_digest = "digest-after", "hpa-1"
    assert await service.observe_workloads({key: with_hpa}, now=now) == 1

    unknown = WorkloadSnapshot(namespace="prod", kind="Deployment", name="api", uid="uid-api", generation=2)
    unknown.spec_digest, unknown.hpa_observed = "digest-after", False
    assert await service.observe_workloads({key: unknown}, now=now + timedelta(minutes=5)) == 0
    assert (await change_repo.latest_spec_observations())[key].hpa_digest == "hpa-1"

    # A listing that succeeds without an HPA for the workload is a real removal.
    removed = WorkloadSnapshot(namespace="prod", kind="Deployment", name="api", uid="uid-api", generation=2)
    removed.spec_digest = "digest-after"
    assert await service.observe_workloads({key: removed}, now=now + timedelta(minutes=10)) == 1
    assert (await change_repo.latest_spec_observations())[key].hpa_digest is None


async def test_hpa_digests_are_unknown_when_hpas_cannot_be_listed(monkeypatch):
    from greenkube.core import k8s_client
    from greenkube.core.optimization import applied_detector

    class FailingApi:
        async def list_horizontal_pod_autoscaler_for_all_namespaces(self):
            raise RuntimeError("403 forbidden")

    async def failing():
        return FailingApi()

    monkeypatch.setattr(k8s_client, "get_autoscaling_v2_api", failing)
    assert await applied_detector._read_hpa_digests() is None

    class EmptyApi:
        async def list_horizontal_pod_autoscaler_for_all_namespaces(self):
            return SimpleNamespace(items=[])

    async def empty():
        return EmptyApi()

    monkeypatch.setattr(k8s_client, "get_autoscaling_v2_api", empty)
    assert await applied_detector._read_hpa_digests() == {}
