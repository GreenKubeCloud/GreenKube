"""Recommendation v2 columns, revisions and plan pins on SQLite and PostgreSQL (E4, E7, migrations 0068/0069)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.recommendation_v2 import FLAT_FIELDS, flat_fields, to_v2, v2_fields
from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

CLUSTER = "prod-eu"
MIB = 1024 * 1024


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_revisions_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "revisions.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _repo(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresRecommendationRepository(manager)
    return SQLiteRecommendationRepository(manager)


async def _set_action_status(manager: DatabaseManager, rec_id: int, action_status: str, status: str = "active"):
    async with manager.connection_scope() as conn:
        if manager.db_type == "postgres":
            await conn.execute(
                "UPDATE recommendation_history SET action_status = $1, status = $2 WHERE id = $3",
                action_status,
                status,
                rec_id,
            )
        else:
            await conn.execute(
                "UPDATE recommendation_history SET action_status = ?, status = ? WHERE id = ?",
                (action_status, status, rec_id),
            )
            await conn.commit()


def _id(record: RecommendationRecord) -> int:
    assert record.id is not None
    return record.id


async def _get(repo, rec_id: int) -> RecommendationRecord:
    record = await repo.get_recommendation_by_id(rec_id)
    assert record is not None
    return record


def _cpu(cpu: int, **overrides) -> RecommendationRecord:
    values = dict(
        pod_name="api",
        namespace="shop",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU,
        description=f"CPU request 500m -> {cpu}m",
        reason="p95 usage is low",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=cpu,
        potential_savings_cost=12.5,
        fingerprint="fp-cpu",
        sources=["greenkube"],
    )
    values.update(overrides)
    return RecommendationRecord(**values)


def _legacy_rows(now: datetime) -> list[RecommendationRecord]:
    """Rows written before v2: every status of the lifecycle, including wave 3a pr_open and verifying."""
    return [
        _cpu(300, fingerprint="fp-active"),
        _cpu(
            200,
            fingerprint="fp-pr-open",
            pod_name="checkout",
            owner_name="checkout",
            status=RecommendationStatus.PR_OPEN,
        ),
        _cpu(
            250,
            fingerprint="fp-applied",
            pod_name="cart",
            owner_name="cart",
            status=RecommendationStatus.APPLIED,
            applied_at=now - timedelta(days=2),
            actual_cpu_request_millicores=250,
            application_method="pr_merge",
        ),
        RecommendationRecord(
            pod_name="search",
            namespace="shop",
            container_name="app",
            type=RecommendationType.RIGHTSIZING_MEMORY,
            description="memory",
            scope="workload",
            owner_kind="StatefulSet",
            owner_name="search",
            current_memory_request_bytes=1024 * MIB,
            recommended_memory_request_bytes=512 * MIB,
            fingerprint="fp-verifying",
            status=RecommendationStatus.VERIFYING,
            applied_at=now - timedelta(days=1),
            verification_status="pending",
        ),
        RecommendationRecord(
            pod_name="pv-1",
            type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
            description="Released volume",
            reason="unbound for 9 days",
            scope="cluster",
            fingerprint="fp-pv",
        ),
        RecommendationRecord(
            pod_name="api",
            namespace="shop",
            type=RecommendationType.OFF_PEAK_SCALING,
            description="Scale down at night",
            scope="workload",
            owner_kind="Deployment",
            owner_name="api",
            cron_schedule="Scale to 0: 20:00-07:00 Europe/Paris",
            fingerprint="fp-offpeak",
        ),
    ]


class TestV2Backfill:
    async def test_backfill_is_lossless_on_every_status(self, db):
        repo = _repo(db)
        now = datetime.now(timezone.utc)
        await repo.save_recommendations(_legacy_rows(now))
        before = {
            record.fingerprint: record
            for record in await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
        }
        assert len(before) == 6
        assert all(record.fingerprint_v4 is None and record.target is None for record in before.values())
        assert all(record.revision == 1 for record in before.values())

        assert await repo.backfill_v2_fields(CLUSTER, batch_size=2) == 6
        assert await repo.backfill_v2_fields(CLUSTER) == 0

        after = {
            record.fingerprint: record
            for record in await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
        }
        for fingerprint, old in before.items():
            new = after[fingerprint]
            # Nothing of the v1 record changed: flat fields, status and lifecycle.
            assert new.model_dump(exclude={"target", "proposal", "fingerprint_v4"}) == old.model_dump(
                exclude={"target", "proposal", "fingerprint_v4"}
            )
            expected = v2_fields(old, CLUSTER)
            assert new.target == expected["target"]
            assert new.proposal == expected["proposal"]
            assert new.fingerprint_v4 == expected["fingerprint_v4"]
            # v1 -> v2 -> v1 round trip of the backfilled row.
            flat = flat_fields(to_v2(new, CLUSTER))
            assert {name: flat.get(name) for name in FLAT_FIELDS} == new.model_dump(
                mode="json", include=set(FLAT_FIELDS)
            )

        assert (after["fp-active"].target or {})["kind"] == "Deployment"
        assert (after["fp-pv"].proposal or {})["kind"] == "deletion"
        assert after["fp-offpeak"].proposal == {
            "kind": "schedule",
            "timezone": "Europe/Paris",
            "sleep_at": "20:00",
            "wake_at": "07:00",
            "weekdays": (after["fp-offpeak"].proposal or {})["weekdays"],
            "replicas_while_asleep": 0,
        }
        assert after["fp-pr-open"].status is RecommendationStatus.PR_OPEN
        assert after["fp-verifying"].status is RecommendationStatus.VERIFYING

    async def test_backfill_keeps_v2_columns_already_written(self, db):
        repo = _repo(db)
        record = _cpu(300, target={"cluster_uid": "other", "kind": "Deployment", "name": "api"})
        await repo.save_recommendations([record])
        await repo.backfill_v2_fields(CLUSTER)
        [stored] = await repo.get_active_recommendations()
        assert stored.target == {"cluster_uid": "other", "kind": "Deployment", "name": "api"}
        assert stored.fingerprint_v4 is not None


class TestRevisions:
    async def test_a_changed_proposal_is_a_new_revision(self, db):
        repo = _repo(db)
        await repo.upsert_recommendations([_cpu(300)])
        [first] = await repo.get_active_recommendations()
        first_id = _id(first)
        assert first.revision == 1

        # Same advice, drifting observation and projection: same revision, no history.
        await repo.upsert_recommendations([_cpu(300, current_cpu_request_millicores=510, potential_savings_cost=13.0)])
        [same] = await repo.get_active_recommendations()
        assert (same.id, same.revision, same.current_cpu_request_millicores) == (first_id, 1, 510)
        assert await repo.get_revisions(first_id) == []

        await repo.upsert_recommendations([_cpu(250, consensus={"policy": "safest", "value": 250})])
        [second] = await repo.get_active_recommendations()
        assert (second.id, second.revision, second.recommended_cpu_request_millicores) == (first_id, 2, 250)
        [superseded] = await repo.get_revisions(first_id)
        assert (superseded.revision, superseded.state) == (1, "superseded")
        assert superseded.snapshot["recommended_cpu_request_millicores"] == 300
        assert superseded.proposal is not None and superseded.proposal["proposed"]["cpu_request_m"] == 300

    async def test_a_record_never_changes_under_an_open_plan(self, db):
        repo = _repo(db)
        await repo.upsert_recommendations([_cpu(300)])
        [record] = await repo.get_active_recommendations()
        rec_id = _id(record)
        await _set_action_status(db, rec_id, "plan_ready")

        await repo.upsert_recommendations([_cpu(200)])
        await repo.upsert_recommendations([_cpu(180)])

        frozen = await _get(repo, rec_id)
        assert (frozen.revision, frozen.recommended_cpu_request_millicores) == (1, 300)
        queued = await repo.get_queued_revisions([rec_id])
        assert list(queued) == [rec_id]
        assert (queued[rec_id].revision, queued[rec_id].state) == (2, "queued")
        assert queued[rec_id].snapshot["recommended_cpu_request_millicores"] == 180
        assert [revision.state for revision in await repo.get_revisions(rec_id)] == ["queued"]

        # Advice back to the plan's: nothing is queued any more.
        await repo.upsert_recommendations([_cpu(300)])
        assert await repo.get_queued_revisions([rec_id]) == {}

        # Plan closed: the next scan promotes the newer advice.
        await repo.upsert_recommendations([_cpu(180)])
        await _set_action_status(db, rec_id, "closed")
        await repo.upsert_recommendations([_cpu(180)])
        promoted = await _get(repo, rec_id)
        assert (promoted.revision, promoted.recommended_cpu_request_millicores) == (2, 180)
        assert [(r.revision, r.state) for r in await repo.get_revisions(rec_id)] == [(1, "superseded")]

    async def test_newer_advice_on_a_pr_open_record_is_queued(self, db):
        repo = _repo(db)
        await repo.upsert_recommendations([_cpu(300)])
        [record] = await repo.get_active_recommendations()
        rec_id = _id(record)
        await _set_action_status(db, rec_id, "pr_open", status="pr_open")

        await repo.upsert_recommendations([_cpu(240)])

        frozen = await _get(repo, rec_id)
        assert (frozen.status, frozen.revision, frozen.recommended_cpu_request_millicores) == (
            RecommendationStatus.PR_OPEN,
            1,
            300,
        )
        queued = await repo.get_queued_revisions([rec_id])
        assert queued[rec_id].snapshot["recommended_cpu_request_millicores"] == 240
        # No duplicate active record was created.
        assert await repo.get_active_recommendations() == []


class TestPlanPins:
    async def test_the_first_pin_of_a_plan_wins(self, db):
        repo = _repo(db)
        await repo.upsert_recommendations([_cpu(300)])
        [record] = await repo.get_active_recommendations()
        rec_id = _id(record)

        assert await repo.pin_plan_revisions("plan-1", {rec_id: 1}) == {rec_id: 1}
        assert await repo.pin_plan_revisions("plan-1", {rec_id: 2}) == {rec_id: 1}
        assert await repo.get_plan_pins("plan-1") == {rec_id: 1}
        assert await repo.get_plan_pins("plan-unknown") == {}
