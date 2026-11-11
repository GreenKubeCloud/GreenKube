# tests/core/optimization/test_applied_detector_rollout.py
"""Rollout-completion apply detection (C0.12, BUG-38).

``applied_at`` used to be the detection time, so a change detected mid-rollout
started its after-window while old pods were still serving. A change is now
applied only once every replica runs the new template, and ``applied_at`` is
the rollout completion time.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization.applied_detector import (
    AppliedDetector,
    K8sWorkloadReader,
    WorkloadSnapshot,
    rollout_complete,
    rollout_completion_time,
)
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


class FakeReader:
    def __init__(self, snapshot: WorkloadSnapshot):
        self.snapshot = snapshot

    async def read(self):
        return {(self.snapshot.namespace, self.snapshot.kind, self.snapshot.name): self.snapshot}


def _record() -> RecommendationRecord:
    return RecommendationRecord(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        # The recommendation existed before the change rolled out.
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )


def _snapshot(**rollout) -> WorkloadSnapshot:
    return WorkloadSnapshot(
        "prod",
        "Deployment",
        "api",
        300,
        0,
        container_requests={"api": (300, 0)},
        generation=4,
        observed_generation=4,
        **rollout,
    )


class TestRolloutComplete:
    def test_readers_without_rollout_state_are_trusted_on_generation(self):
        assert rollout_complete(WorkloadSnapshot("prod", "Deployment", "api"))
        assert not rollout_complete(WorkloadSnapshot("prod", "Deployment", "api", generation=5, observed_generation=4))

    @pytest.mark.parametrize(
        ("desired", "current", "updated", "available", "complete"),
        [
            (3, 3, 3, 3, True),
            (3, 4, 3, 3, False),  # an old pod is still running
            (3, 3, 2, 3, False),  # not every replica runs the new template
            (3, 3, 3, 2, False),  # a new replica is not available yet
            (0, 0, 0, 0, True),  # scaled to zero
        ],
    )
    def test_replica_counts(self, desired, current, updated, available, complete):
        snapshot = _snapshot(
            desired_replicas=desired,
            current_replicas=current,
            updated_replicas=updated,
            available_replicas=available,
        )
        assert rollout_complete(snapshot) is complete

    def test_statefulset_revisions_must_converge(self):
        snapshot = _snapshot(
            desired_replicas=2,
            current_replicas=2,
            updated_replicas=2,
            available_replicas=2,
            current_revision="api-1",
            update_revision="api-2",
        )
        assert not rollout_complete(snapshot)

    def test_completion_time_prefers_the_progressing_condition(self):
        now = datetime.now(timezone.utc)
        done = now - timedelta(minutes=20)
        assert rollout_completion_time(_snapshot(rollout_completed_at=done), now) == (done, "progressing_condition")
        assert rollout_completion_time(_snapshot(), now) == (now, "detection_time")
        future = now + timedelta(minutes=5)
        assert rollout_completion_time(_snapshot(rollout_completed_at=future), now) == (now, "detection_time")


class TestDetection:
    async def test_detection_waits_for_rollout_completion(self, repo):
        await repo.save_recommendations([_record()])
        rolling = _snapshot(desired_replicas=3, current_replicas=4, updated_replicas=2, available_replicas=3)
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=FakeReader(rolling), config=Config())

        assert await detector.detect() == []
        assert (await repo.get_active_recommendations())[0].status is RecommendationStatus.ACTIVE

    async def test_applied_at_is_rollout_completion_time(self, repo):
        await repo.save_recommendations([_record()])
        done = datetime.now(timezone.utc) - timedelta(minutes=12)
        complete = _snapshot(
            desired_replicas=3,
            current_replicas=3,
            updated_replicas=3,
            available_replicas=3,
            rollout_completed_at=done,
        )
        detector = AppliedDetector(RecommendationLifecycle(repo), reader=FakeReader(complete), config=Config())

        applied = await detector.detect()

        assert len(applied) == 1
        assert applied[0].applied_at == done
        assert applied[0].baseline is not None
        baseline = applied[0].baseline.model_dump()
        assert baseline["rollout"]["completion_source"] == "progressing_condition"
        assert datetime.fromisoformat(baseline["baseline_window_end"]) <= done


class TestKubernetesReader:
    async def test_reader_reports_rollout_state_per_kind(self, monkeypatch):
        import greenkube.core.k8s_client as k8s_client

        completed = datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc)

        def pod_template():
            return SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[SimpleNamespace(name="app", resources=SimpleNamespace(requests={"cpu": "300m"}))]
                )
            )

        deployment = SimpleNamespace(
            metadata=SimpleNamespace(namespace="prod", name="api", uid="u1", generation=3),
            spec=SimpleNamespace(replicas=2, template=pod_template()),
            status=SimpleNamespace(
                observed_generation=3,
                replicas=2,
                updated_replicas=2,
                available_replicas=2,
                conditions=[
                    SimpleNamespace(
                        type="Available", status="True", reason="MinimumReplicasAvailable", last_update_time=None
                    ),
                    SimpleNamespace(
                        type="Progressing",
                        status="True",
                        reason="NewReplicaSetAvailable",
                        last_update_time=completed,
                    ),
                ],
            ),
        )
        statefulset = SimpleNamespace(
            metadata=SimpleNamespace(namespace="prod", name="db", uid="u2", generation=2),
            spec=SimpleNamespace(replicas=3, template=pod_template()),
            status=SimpleNamespace(
                observed_generation=2,
                replicas=3,
                updated_replicas=3,
                available_replicas=None,
                ready_replicas=3,
                current_revision="db-7",
                update_revision="db-7",
            ),
        )
        daemonset = SimpleNamespace(
            metadata=SimpleNamespace(namespace="prod", name="agent", uid="u3", generation=5),
            spec=SimpleNamespace(template=pod_template()),
            status=SimpleNamespace(
                observed_generation=5,
                desired_number_scheduled=4,
                current_number_scheduled=4,
                updated_number_scheduled=3,
                number_available=4,
            ),
        )

        def lister(item):
            async def list_items():
                return SimpleNamespace(items=[item])

            return list_items

        async def get_api():
            return SimpleNamespace(
                list_deployment_for_all_namespaces=lister(deployment),
                list_stateful_set_for_all_namespaces=lister(statefulset),
                list_daemon_set_for_all_namespaces=lister(daemonset),
            )

        monkeypatch.setattr(k8s_client, "get_apps_v1_api", get_api)

        snapshots = await K8sWorkloadReader().read()

        api = snapshots[("prod", "Deployment", "api")]
        assert (api.desired_replicas, api.updated_replicas, api.available_replicas) == (2, 2, 2)
        assert api.rollout_completed_at == completed
        assert rollout_complete(api)
        db = snapshots[("prod", "StatefulSet", "db")]
        assert db.available_replicas == 3 and db.current_revision == db.update_revision == "db-7"
        assert rollout_complete(db)
        agent = snapshots[("prod", "DaemonSet", "agent")]
        assert (agent.desired_replicas, agent.updated_replicas) == (4, 3)
        assert not rollout_complete(agent)
