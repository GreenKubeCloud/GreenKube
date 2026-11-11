"""Repair of phantom "detected" changes written by the old apply detector, on SQLite and PostgreSQL.

The old detector recorded a rightsizing as applied (``detected``) while the
live request never moved from the baseline (20Mi -> 23Mi within tolerance of
the untouched 20Mi), or matched a rollout that predates the recommendation.
The repair retires them (``stale`` with a ``retired`` event), supersedes their
ledger rows and closes their change measurement, and only with evidence.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

import pytest

from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.phantom_repair import PHANTOM_CODE, PhantomChangeRepair
from greenkube.core.workload_usage import WorkloadUsageRollup, floor_hour
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.savings import SavingsLedgerRecord
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository
from greenkube.storage.change_repository import ChangeRepository
from greenkube.storage.postgres.savings_repository import PostgresSavingsLedgerRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository
from greenkube.verify.model import MeasurementState
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.storage.test_workload_usage_repository import _metrics_repo, _row  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import change_event, recommendation_repo  # pyrefly: ignore[missing-import]

MIB = 1024 * 1024


def _savings_repo(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresSavingsLedgerRepository(manager)
    return SQLiteSavingsLedgerRepository(manager)


def _rec(name: str, *, current: int, recommended: int, created_at: datetime) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name=name,
        namespace="shop",
        container_name="web",
        owner_kind="Deployment",
        owner_name=name,
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="rightsize memory",
        current_memory_request_bytes=current,
        recommended_memory_request_bytes=recommended,
        potential_savings_cost=12.0,
        created_at=created_at,
    )


class World:
    def __init__(self, manager: DatabaseManager):
        self.manager = manager
        self.repo = recommendation_repo(manager)
        self.lifecycle = RecommendationLifecycle(self.repo)
        self.savings = _savings_repo(manager)
        self.changes = ChangeRepository(manager)
        self.usage = create_workload_usage_repository(manager, manager.db_type)

    async def detected(self, rec: RecommendationRecord, *, actual: int, applied_at: datetime, rollout_at: datetime):
        await self.repo.save_recommendations([rec])
        (stored,) = [r for r in await self.repo.get_active_recommendations() if r.pod_name == rec.pod_name]
        assert stored.id is not None
        applied = await self.lifecycle.apply(
            stored.id,
            actual_memory=actual,
            application_method="detected",
            actor="detector",
            applied_at=applied_at,
            rollout={"completed_at": rollout_at.isoformat(), "completion_source": "deployment_progressing"},
        )
        await self.savings.save_records(
            [
                SavingsLedgerRecord(
                    recommendation_id=stored.id,
                    recommendation_type=rec.type.value,
                    namespace="shop",
                    cost_saved_dollars=0.5,
                    co2e_saved_grams=3.0,
                    period_start=applied_at,
                    period_end=applied_at + timedelta(hours=1),
                    slot_start=applied_at,
                )
            ]
        )
        return applied

    async def live_requests(self, requests: dict[str, int], start: datetime, hours: int) -> None:
        rows = []
        for name, request in requests.items():
            for step in range(12 * hours):
                ts = start + timedelta(minutes=5 * step)
                pod = f"{name}-7d9f8b6c5d-abcde"
                rows.append(_row(ts, pod_name=pod, owner_name=name, memory_request=request))
                rows.append(
                    _row(ts, pod_name=pod, owner_name=name, node=None, container_name="web", memory_request=request)
                )
        await _metrics_repo(self.manager).write_combined_metrics(rows)
        await WorkloadUsageRollup(self.usage).refresh(now=start + timedelta(hours=hours, minutes=1))

    async def ledger_superseded(self, rec_id: int) -> list:
        table = "recommendation_savings_ledger"
        async with self.manager.connection_scope() as connection:
            conn: Any = connection
            if self.manager.db_type == "postgres":
                rows = await conn.fetch(f"SELECT superseded FROM {table} WHERE recommendation_id = $1", rec_id)
                return [bool(row["superseded"]) for row in rows]
            async with conn.execute(f"SELECT superseded FROM {table} WHERE recommendation_id = ?", (rec_id,)) as cur:
                return [bool(row[0]) for row in await cur.fetchall()]


@pytest.fixture
async def world(database) -> AsyncGenerator[World, None]:  # noqa: F811
    yield World(database)


async def test_phantom_detected_changes_are_retired_with_their_ledger_and_measurement(world: World):
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=3)
    applied_at = floor_hour(now - timedelta(hours=5))

    # Phantom: 20Mi -> 23Mi recorded while the live request stayed 20Mi.
    phantom = await world.detected(
        _rec("phantom", current=20 * MIB, recommended=23 * MIB, created_at=created),
        actual=20 * MIB,
        applied_at=applied_at,
        rollout_at=applied_at,
    )
    # A real change whose actual_* an old rescan reverted: the live request is the proposal.
    reverted = await world.detected(
        _rec("reverted", current=400 * MIB, recommended=200 * MIB, created_at=created),
        actual=400 * MIB,
        applied_at=applied_at,
        rollout_at=applied_at,
    )
    # The matched rollout completed before the recommendation existed.
    early = await world.detected(
        _rec("early", current=300 * MIB, recommended=150 * MIB, created_at=created),
        actual=150 * MIB,
        applied_at=applied_at,
        rollout_at=created - timedelta(days=30),
    )
    # A genuine detected change: untouched.
    genuine = await world.detected(
        _rec("genuine", current=512 * MIB, recommended=256 * MIB, created_at=created),
        actual=256 * MIB,
        applied_at=applied_at,
        rollout_at=applied_at,
    )
    names = ("phantom", "reverted", "early", "genuine")
    ids = {name: rec.id for name, rec in zip(names, (phantom, reverted, early, genuine))}
    assert all(value is not None for value in ids.values())
    for name in ("phantom", "genuine"):
        await world.changes.insert_change(
            change_event(
                applied_at,
                change_id=f"change-{name}",
                recommendation_ids=[int(ids[name] or 0)],
                name=name,
                spec_digest_after=f"digest-{name}",
            ).model_copy(update={"workload_uid": f"uid-{name}"})
        )

    await world.live_requests(
        {"phantom": 20 * MIB, "reverted": 200 * MIB, "early": 150 * MIB, "genuine": 256 * MIB},
        applied_at + timedelta(hours=1),
        hours=3,
    )

    repair = PhantomChangeRepair(world.repo, usage=world.usage, savings=world.savings, changes=world.changes)
    result = await repair.run(now=now)

    assert sorted(result.retired) == sorted([int(ids["phantom"] or 0), int(ids["early"] or 0)])
    # "reverted" looked like a phantom from its row, but its live request is the proposal: left alone.
    assert result.unproven == [ids["reverted"]]
    assert result.closed_changes == ["change-phantom"]
    for name in ("phantom", "early"):
        record = await world.repo.get_recommendation_by_id(int(ids[name] or 0))
        assert record is not None and record.status == RecommendationStatus.STALE
        assert record.cost_saved is None and record.carbon_saved_co2e_grams is None
        events = await world.repo.get_events(int(ids[name] or 0))
        retired = [event for event in events if event.event_type == "retired"]
        assert len(retired) == 1
        assert await world.ledger_superseded(int(ids[name] or 0)) == [True]
    for name in ("reverted", "genuine"):
        record = await world.repo.get_recommendation_by_id(int(ids[name] or 0))
        assert record is not None and record.status == RecommendationStatus.APPLIED
        assert await world.ledger_superseded(int(ids[name] or 0)) == [False]

    closed = await world.changes.get_status("change-phantom")
    assert closed is not None and closed.state is MeasurementState.CLOSED
    events = await world.changes.list_events("change-phantom")
    assert events[-1].event_type == "retired" and PHANTOM_CODE in events[-1].reason
    kept = await world.changes.get_status("change-genuine")
    assert kept is not None and kept.state is MeasurementState.APPLIED

    # Idempotent: a second run changes nothing.
    again = await repair.run(now=now)
    assert again.retired == [] and again.closed_changes == []


async def test_without_recent_usage_evidence_nothing_is_retired(world: World):
    now = datetime.now(timezone.utc)
    applied_at = floor_hour(now - timedelta(hours=5))
    phantom = await world.detected(
        _rec("phantom", current=20 * MIB, recommended=23 * MIB, created_at=now - timedelta(days=3)),
        actual=20 * MIB,
        applied_at=applied_at,
        rollout_at=applied_at,
    )
    repair = PhantomChangeRepair(world.repo, usage=world.usage, savings=world.savings, changes=world.changes)
    result = await repair.run(now=now)
    assert result.retired == [] and result.unproven == [phantom.id]
    record = await world.repo.get_recommendation_by_id(int(phantom.id or 0))
    assert record is not None and record.status == RecommendationStatus.APPLIED
