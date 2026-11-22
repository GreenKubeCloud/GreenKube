"""GET /api/v1/sources, /sources/{id}/runs and /sources/{id}/signals (roadmap E5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar, Dict, FrozenSet, List, Optional

import pytest

from greenkube.api.routers.sources import get_signal_repository, get_source_catalog
from greenkube.core.config import Config
from greenkube.core.optimization.registry import build_source_registry
from greenkube.signals.model import AlgorithmInfo, Capability, DeletionProposal, SourceSignal, TargetRef
from greenkube.signals.runtime import SourceRunRecord, SourceRunStatus
from greenkube.signals.sdk import Source, SourceBatch, SourceHealthStatus, SourceTier
from greenkube.storage.base_signal_repository import SignalRepository, SourceRunSummary, StoredSignal


class AcmeSource(Source):
    """Acme's container recommender."""

    id: ClassVar[str] = "acme"
    display_name: ClassVar[str] = "Acme recommender"
    tier: ClassVar[SourceTier] = SourceTier.COMMERCIAL
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CLEANUP})
    version: ClassVar[Optional[str]] = "0.3.1"

    async def collect(self, ctx):
        return SourceBatch()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _run(source_id: str, status: SourceRunStatus, run_id: int, **kw) -> SourceRunRecord:
    started = _now() - timedelta(minutes=run_id)
    values = dict(id=run_id, source_id=source_id, status=status, started_at=started, finished_at=started)
    values.update(kw)
    return SourceRunRecord(**values)


class FakeSignalRepository(SignalRepository):
    def __init__(self) -> None:
        self.runs: Dict[str, List[SourceRunRecord]] = {}
        self.signals: Dict[str, List[StoredSignal]] = {}

    async def record_run(self, run, signals=()):
        raise NotImplementedError

    async def latest_runs(self):
        return {
            source_id: SourceRunSummary(
                latest=runs[0],
                last_success_at=next((r.finished_at for r in runs if r.status == SourceRunStatus.SUCCEEDED), None),
            )
            for source_id, runs in self.runs.items()
        }

    async def list_runs(self, source_id, *, limit=20):
        return self.runs.get(source_id, [])[:limit]

    async def list_signals(self, source_id, *, limit=100, target_key=None):
        items = self.signals.get(source_id, [])
        if target_key is not None:
            items = [item for item in items if item.signal.target_key == target_key]
        return items[:limit]

    async def prune(self, before):
        return 0


def _point():
    return SimpleNamespace(
        name="acme", value="acme_source:AcmeSource", load=lambda: AcmeSource, dist=SimpleNamespace(name="acme-source")
    )


def _broken_point():
    def load():
        raise ImportError("No module named 'acme_sdk'")

    return SimpleNamespace(name="broken", value="broken:Source", load=load, dist=None)


@pytest.fixture
def repo(client):
    repository = FakeSignalRepository()
    config = Config().model_copy(
        update={
            "SOURCES_CONFIG": '{"greenkube.orphaned_pv": {"enabled": false}, "vpa": {"enabled": "true"}}',
        }
    )
    catalog = build_source_registry(config, entry_points=lambda: [_point(), _broken_point()])
    client.app.dependency_overrides[get_signal_repository] = lambda: repository
    client.app.dependency_overrides[get_source_catalog] = lambda: catalog
    yield repository
    client.app.dependency_overrides.pop(get_signal_repository, None)
    client.app.dependency_overrides.pop(get_source_catalog, None)


def _signal() -> SourceSignal:
    return SourceSignal(
        source_id="acme",
        target=TargetRef.for_kind("c1", "PersistentVolume", "pv-1"),
        capability=Capability.CLEANUP,
        kind="ORPHANED_PERSISTENT_VOLUME",
        proposal=DeletionProposal(reason="Released"),
        algorithm=AlgorithmInfo(name="acme.cleanup"),
    )


class TestListSources:
    def test_lists_every_source_with_status_health_and_counts(self, client, repo):
        repo.runs = {
            "vpa": [
                _run(
                    "vpa",
                    SourceRunStatus.UNAVAILABLE,
                    1,
                    health_status=SourceHealthStatus.UNAVAILABLE,
                    health_reason="VPA CRD not installed",
                )
            ],
            "greenkube.rightsizing_cpu": [
                _run("greenkube.rightsizing_cpu", SourceRunStatus.SUCCEEDED, 2, signal_count=4, recommendation_count=4)
            ],
            "greenkube.orphaned_lb": [
                _run(
                    "greenkube.orphaned_lb",
                    SourceRunStatus.FAILED,
                    3,
                    error_code="RuntimeError",
                    error="RuntimeError",
                    consecutive_failures=2,
                ),
                _run("greenkube.orphaned_lb", SourceRunStatus.SUCCEEDED, 9),
            ],
            "acme": [_run("acme", SourceRunStatus.SKIPPED, 4, error_code="rate_limited", error="rate limited")],
            "uninstalled": [_run("uninstalled", SourceRunStatus.SUCCEEDED, 5)],
        }

        response = client.get("/api/v1/sources")

        assert response.status_code == 200
        body = response.json()
        by_id = {source["id"]: source for source in body["sources"]}
        assert [s["id"] for s in body["sources"]][:2] == ["vpa", "greenkube.rightsizing_cpu"]
        assert by_id["vpa"]["status"] == "failing"  # explicitly enabled but not installed
        assert "VPA CRD not installed" in by_id["vpa"]["status_reason"]
        assert by_id["greenkube.orphaned_pv"]["status"] == "disabled"
        assert by_id["greenkube.rightsizing_memory"]["status"] == "pending"
        assert "greenkube.nodepool" not in by_id and "greenkube.zombie" not in by_id
        cpu = by_id["greenkube.rightsizing_cpu"]
        assert cpu["status"] == "healthy"
        assert cpu["tier"] == "native" and cpu["legacy_source"] == "greenkube"
        assert cpu["capabilities"] == ["container_requests"]
        assert cpu["signal_count"] == 4 and cpu["last_run"]["run_id"] == 2
        assert cpu["limits"]["collect_timeout_seconds"] == 300.0
        lb = by_id["greenkube.orphaned_lb"]
        assert lb["status"] == "failing" and lb["consecutive_failures"] == 2
        assert lb["last_success_at"] is not None
        acme = by_id["acme"]
        assert acme["origin"] == "plugin" and acme["distribution"] == "acme-source"
        assert acme["tier"] == "commercial" and acme["version"] == "0.3.1"
        assert acme["legacy_source"] is None
        assert acme["status"] == "degraded" and acme["status_reason"] == "rate limited"
        assert by_id["uninstalled"]["origin"] == "unregistered"
        assert body["sources"][-1]["id"] == "uninstalled"
        [problem] = body["problems"]
        assert problem["code"] == "plugin_load_failed" and "acme_sdk" in problem["message"]

    def test_storage_outage_is_a_503(self, client, repo):
        async def boom():
            raise ConnectionError("db down")

        repo.latest_runs = boom
        assert client.get("/api/v1/sources").status_code == 503


class TestSourceRuns:
    def test_returns_runs_newest_first(self, client, repo):
        repo.runs = {
            "acme": [
                _run("acme", SourceRunStatus.FAILED, 2, error_code="timeout"),
                _run("acme", SourceRunStatus.SUCCEEDED, 1),
            ]
        }

        response = client.get("/api/v1/sources/acme/runs?limit=1")

        assert response.status_code == 200
        [run] = response.json()
        assert run["run_id"] == 2 and run["status"] == "failed" and run["error_code"] == "timeout"

    def test_known_source_without_runs_is_empty(self, client, repo):
        assert client.get("/api/v1/sources/greenkube.rightsizing_memory/runs").json() == []

    def test_unknown_source_is_404(self, client, repo):
        assert client.get("/api/v1/sources/nope/runs").status_code == 404
        assert client.get("/api/v1/sources/nope/signals").status_code == 404

    def test_track_record_route_is_not_shadowed(self, client, repo, mock_savings_repo):
        assert client.get("/api/v1/sources/track-record").status_code != 404


class TestSourceSignals:
    def test_returns_signals_with_their_sighting_window(self, client, repo):
        signal = _signal()
        now = _now()
        repo.signals = {
            "acme": [
                StoredSignal(
                    signal=signal,
                    first_seen_at=now - timedelta(days=2),
                    last_seen_at=now,
                    last_run_id=7,
                    recommendation_fingerprint=None,
                )
            ]
        }

        response = client.get("/api/v1/sources/acme/signals", params={"target_key": signal.target_key})

        assert response.status_code == 200
        [item] = response.json()
        assert item["signal"]["signal_id"] == signal.signal_id
        assert item["signal"]["proposal"] == {"kind": "deletion", "reason": "Released"}
        assert item["last_run_id"] == 7
