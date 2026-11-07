"""API contract for optimization runs (E1, E10) and enqueue-only refreshes (BUG-21)."""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from greenkube.api.routers.optimization import RUN_ID_HEADER, get_optimization_run_repository
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunCompletion,
    OptimizationRunDiff,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunOutcome,
    SourceRunStatus,
)
from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository

T0 = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


class InMemoryRunRepository(OptimizationRunRepository):
    """Minimal queue with the same dedup semantics as the SQL backends."""

    def __init__(self) -> None:
        self.runs: Dict[int, OptimizationRun] = {}
        self.enqueue_calls: List[dict] = []
        self.fail_enqueue = False

    async def create(self, run):
        run_id = len(self.runs) + 1
        run = run.model_copy(update={"id": run_id})
        self.runs[run_id] = run
        return run

    async def finish(self, run_id, completion: OptimizationRunCompletion):
        run = self.runs[run_id].model_copy(update=completion.model_dump())
        self.runs[run_id] = run
        return run

    async def get(self, run_id):
        return self.runs.get(run_id)

    async def latest(self, namespace=None):
        runs = self.list_sync()
        return runs[0] if runs else None

    def list_sync(self) -> List[OptimizationRun]:
        return sorted(self.runs.values(), key=lambda run: (run.requested_at, run.id), reverse=True)

    async def list_runs(self, limit=20):
        return self.list_sync()[:limit]

    async def enqueue(self, *, namespace, trigger, requested_at, requested_by=None) -> Tuple[OptimizationRun, bool]:
        self.enqueue_calls.append({"namespace": namespace, "trigger": trigger, "requested_by": requested_by})
        if self.fail_enqueue:
            raise ConnectionError("database unavailable")
        for run in self.runs.values():
            if run.status == OptimizationRunStatus.QUEUED and run.namespace in (None, namespace):
                return run, False
        run_id = len(self.runs) + 1
        run = OptimizationRun(
            id=run_id,
            status=OptimizationRunStatus.QUEUED,
            namespace=namespace,
            trigger=trigger,
            requested_at=requested_at,
            requested_by=requested_by,
        )
        self.runs[run_id] = run
        return run, True

    async def enqueue_if_due(self, *, requested_at, due_before, requested_by=None) -> Optional[OptimizationRun]:
        raise AssertionError("the API never schedules runs")

    async def claim_next(self, *, claimed_by, started_at):
        raise AssertionError("the API never claims runs")

    async def fail_abandoned(self, *, started_before, completed_at, error):
        raise AssertionError("the API never reaps runs")


@pytest.fixture
def run_repo(client):
    repo = InMemoryRunRepository()
    client.app.dependency_overrides[get_optimization_run_repository] = lambda: repo
    return repo


@pytest.fixture
def no_scan(monkeypatch):
    """Fail loudly if any API request runs the optimizer."""
    run_scan = AsyncMock(side_effect=AssertionError("GET endpoints must not scan"))
    monkeypatch.setattr("greenkube.core.optimization.engine.OptimizationEngine.run_scan", run_scan)
    return run_scan


def _assert_no_recommendation_writes(reco_repo) -> None:
    reco_repo.upsert_recommendations.assert_not_called()
    reco_repo.reconcile_active_recommendations.assert_not_called()
    reco_repo.save_recommendations.assert_not_called()


class TestRequestRun:
    def test_post_queues_a_run_and_returns_202(self, client, run_repo):
        response = client.post("/api/v1/optimization/runs")

        assert response.status_code == 202
        assert response.json() == {"run_id": 1, "status": "queued"}
        assert response.headers["Location"] == "/api/v1/optimization/runs/1"
        assert run_repo.enqueue_calls == [
            {"namespace": None, "trigger": OptimizationRunTrigger.MANUAL, "requested_by": "anonymous"}
        ]

    def test_post_is_deduplicated_while_a_run_is_pending(self, client, run_repo):
        first = client.post("/api/v1/optimization/runs").json()
        second = client.post("/api/v1/optimization/runs", json={"namespace": "prod"}).json()

        assert first == second == {"run_id": 1, "status": "queued"}
        assert len(run_repo.runs) == 1

    def test_post_accepts_a_namespace_scope(self, client, run_repo):
        response = client.post("/api/v1/optimization/runs", json={"namespace": "prod"})

        assert response.status_code == 202
        assert run_repo.runs[1].namespace == "prod"

    def test_post_rejects_invalid_namespace(self, client, run_repo):
        response = client.post("/api/v1/optimization/runs", json={"namespace": "Not_Valid!"})

        assert response.status_code == 400
        assert run_repo.runs == {}


class TestReadRuns:
    def test_get_run_exposes_the_contract_fields(self, client, run_repo):
        run_repo.runs[1] = OptimizationRun(
            id=1,
            status=OptimizationRunStatus.SUCCEEDED,
            trigger=OptimizationRunTrigger.SCHEDULED,
            requested_at=T0,
            started_at=T0 + timedelta(seconds=5),
            completed_at=T0 + timedelta(seconds=40),
            recommendation_count=12,
            analyzer_count=3,
            failed_analyzer_count=1,
            new_count=4,
            changed_count=1,
            resolved_count=2,
            unchanged_count=7,
            source_outcomes=[
                SourceRunOutcome(source="greenkube", status=SourceRunStatus.SUCCEEDED, complete=True),
                SourceRunOutcome(source="vpa", status=SourceRunStatus.FAILED, error="timeout"),
            ],
            diff=OptimizationRunDiff(new=["fp-1"]),
            error="Sources failed or incomplete: vpa",
        )

        body = client.get("/api/v1/optimization/runs/1").json()

        for field in (
            "run_id",
            "status",
            "requested_at",
            "started_at",
            "completed_at",
            "recommendation_count",
            "error",
        ):
            assert field in body
        assert body["run_id"] == 1
        assert body["status"] == "succeeded"
        assert body["trigger"] == "scheduled"
        assert body["requested_at"] == "2026-11-07T10:00:00Z"
        assert body["recommendation_count"] == 12
        assert (body["new_count"], body["changed_count"], body["resolved_count"]) == (4, 1, 2)
        assert body["source_count"] == 3 and body["failed_source_count"] == 1
        assert [source["status"] for source in body["sources"]] == ["succeeded", "failed"]
        assert body["diff"]["new"] == ["fp-1"]

    def test_queued_run_has_no_start_or_completion(self, client, run_repo):
        client.post("/api/v1/optimization/runs")

        body = client.get("/api/v1/optimization/runs/1").json()

        assert body["status"] == "queued"
        assert body["started_at"] is None
        assert body["completed_at"] is None

    def test_unknown_run_is_404(self, client, run_repo):
        assert client.get("/api/v1/optimization/runs/999").status_code == 404

    def test_list_runs_newest_first_with_limit(self, client, run_repo):
        for index in range(3):
            run_repo.runs[index + 1] = OptimizationRun(
                id=index + 1,
                status=OptimizationRunStatus.SUCCEEDED,
                trigger=OptimizationRunTrigger.SCHEDULED,
                requested_at=T0 + timedelta(hours=index),
            )

        body = client.get("/api/v1/optimization/runs", params={"limit": 2}).json()

        assert [run["run_id"] for run in body] == [3, 2]
        assert client.get("/api/v1/optimization/runs", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/optimization/runs", params={"limit": 101}).status_code == 422


class TestGetEndpointsNeverWrite:
    """Regression for BUG-21: GET endpoints used to run and persist scans."""

    def test_empty_first_read_no_longer_scans(self, client, mock_reco_repo, run_repo, no_scan):
        response = client.get("/api/v1/recommendations")

        assert response.status_code == 200
        assert response.json() == []
        assert RUN_ID_HEADER not in response.headers
        no_scan.assert_not_awaited()
        _assert_no_recommendation_writes(mock_reco_repo)
        assert run_repo.enqueue_calls == []

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/recommendations", "/api/v1/recommendations/active", "/api/v1/recommendations/top"],
    )
    def test_refresh_only_enqueues_and_returns_the_run_id(self, client, mock_reco_repo, run_repo, no_scan, path):
        record = RecommendationRecord(
            id=5,
            pod_name="api",
            namespace="prod",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Reduce CPU",
            potential_savings_co2e_grams=10.0,
            potential_savings_cost=2.0,
        )
        mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[record])
        mock_reco_repo.get_top_recommendations = AsyncMock(return_value=[record])

        response = client.get(path, params={"namespace": "prod", "refresh": "true"})

        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.headers[RUN_ID_HEADER] == "1"
        assert run_repo.enqueue_calls == [
            {"namespace": "prod", "trigger": OptimizationRunTrigger.REFRESH, "requested_by": "anonymous"}
        ]
        no_scan.assert_not_awaited()
        _assert_no_recommendation_writes(mock_reco_repo)

    def test_repeated_refreshes_share_one_pending_run(self, client, run_repo, no_scan):
        ids = {
            client.get("/api/v1/recommendations/active", params={"refresh": "true"}).headers[RUN_ID_HEADER]
            for _ in range(3)
        }

        assert ids == {"1"}
        assert len(run_repo.runs) == 1

    def test_refresh_still_returns_data_when_the_queue_is_unavailable(self, client, mock_reco_repo, run_repo):
        run_repo.fail_enqueue = True

        response = client.get("/api/v1/recommendations/active", params={"refresh": "true"})

        assert response.status_code == 200
        assert RUN_ID_HEADER not in response.headers
        mock_reco_repo.get_active_recommendations.assert_awaited_once()

    def test_refresh_parameter_is_marked_deprecated_in_openapi(self, client):
        schema = client.get("/api/v1/openapi.json").json()
        parameters = schema["paths"]["/api/v1/recommendations/active"]["get"]["parameters"]
        refresh = next(parameter for parameter in parameters if parameter["name"] == "refresh")

        assert refresh["deprecated"] is True
        assert "/api/v1/optimization/runs" in schema["paths"]
