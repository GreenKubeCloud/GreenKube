"""Revision hysteresis and shared bundle actions across scans (wave-4 e2e, roadmap E7).

The e2e saw 22 memory revisions in 20 minutes from byte-level working-set
jitter, and a CPU+memory pair whose copies of one shared action drifted apart
under an open plan until the plan was cancelled.
"""

from __future__ import annotations

import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.dedup import REVISION_HYSTERESIS, apply_revision_hysteresis, within_hysteresis
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.evidence import build_patch
from greenkube.gitops.contracts import SetContainerResources
from greenkube.models.metrics import Recommendation, RecommendationPatchV2, RecommendationRecord, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from tests.core.optimization.test_container_actions import _recommendation  # pyrefly: ignore[missing-import]

MIB = 1024**2


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hysteresis.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


def _pair(cpu_target: int, memory_target: int) -> list[Recommendation]:
    cpu = _recommendation(recommendation_id=0, container_name="api", cpu_request=1000, cpu_target=cpu_target)
    memory = _recommendation(
        recommendation_id=0,
        container_name="api",
        rec_type=RecommendationType.RIGHTSIZING_MEMORY,
        memory_request=1024 * MIB,
        memory_target=memory_target,
    )
    for rec in (cpu, memory):
        rec.id = None
        rec.description = f"cpu {cpu_target}m, memory {memory_target} bytes"
        rec.patch = RecommendationPatchV2.model_validate(build_patch(rec))
    return [cpu, memory]


async def _records(repo) -> dict[str, RecommendationRecord]:
    return {str(record.type.value): record for record in await repo.get_active_recommendations()}


async def _open_plan(repo, *rec_ids: int) -> None:
    async with repo.db_manager.connection_scope() as conn:
        for rec_id in rec_ids:
            await conn.execute("UPDATE recommendation_history SET action_status = 'plan_ready' WHERE id = ?", (rec_id,))
        await conn.commit()


async def test_byte_level_jitter_creates_no_new_revision(repo):
    engine = OptimizationEngine(sources=[])
    await engine.persist(_pair(300, 256 * MIB), repo)

    # Twenty scans of jitter: a few millicores and bytes, always under 5%.
    for step in range(1, 21):
        await engine.persist(_pair(300 + step % 7, 256 * MIB + step * 4099), repo)

    records = await _records(repo)
    cpu, memory = records["RIGHTSIZING_CPU"], records["RIGHTSIZING_MEMORY"]
    assert (cpu.revision, cpu.recommended_cpu_request_millicores) == (1, 300)
    assert (memory.revision, memory.recommended_memory_request_bytes) == (1, 256 * MIB)
    assert memory.description == "cpu 300m, memory 268435456 bytes"
    assert await repo.get_revisions(memory.id) == []
    assert cpu.action is not None and memory.action is not None
    assert cpu.action.model_dump(mode="json") == memory.action.model_dump(mode="json")


async def test_a_change_above_the_threshold_is_a_new_revision(repo):
    engine = OptimizationEngine(sources=[])
    await engine.persist(_pair(300, 256 * MIB), repo)

    await engine.persist(_pair(300, 300 * MIB), repo)

    memory = (await _records(repo))["RIGHTSIZING_MEMORY"]
    assert (memory.revision, memory.recommended_memory_request_bytes) == (2, 300 * MIB)
    assert memory.action is not None
    [operation] = memory.action.operations
    assert isinstance(operation, SetContainerResources)
    assert operation.resources.requests == {"cpu": "300m", "memory": "300Mi"}


async def test_a_pair_under_an_open_plan_keeps_identical_actions_across_scans(repo):
    engine = OptimizationEngine(sources=[])
    await engine.persist(_pair(300, 256 * MIB), repo)
    before = await _records(repo)
    planned = before["RIGHTSIZING_CPU"].action
    rec_ids = [record.id for record in before.values() if record.id is not None]
    assert planned is not None and sorted(planned.recommendation_ids) == sorted(rec_ids)
    await _open_plan(repo, *rec_ids)

    # The memory advice moves well beyond the hysteresis while the CPU advice stays.
    for memory_mib in (320, 400, 380):
        await engine.persist(_pair(300, memory_mib * MIB), repo)

    after = await _records(repo)
    cpu, memory = after["RIGHTSIZING_CPU"], after["RIGHTSIZING_MEMORY"]
    assert cpu.action is not None and memory.action is not None
    assert cpu.action.model_dump(mode="json") == memory.action.model_dump(mode="json")
    assert cpu.action.model_dump(mode="json") == planned.model_dump(mode="json")
    assert memory.recommended_memory_request_bytes == 256 * MIB
    queued = await repo.get_queued_revisions([memory.id])
    assert queued[memory.id].snapshot["recommended_memory_request_bytes"] == 380 * MIB


class TestWithinHysteresis:
    def test_small_moves_are_held_in_the_same_direction_only(self):
        [cpu, _] = _pair(300, 256 * MIB)
        stored = RecommendationRecord.from_recommendation(cpu)

        assert within_hysteresis(cpu.model_copy(update={"recommended_cpu_request_millicores": 314}), stored)
        assert not within_hysteresis(cpu.model_copy(update={"recommended_cpu_request_millicores": 320}), stored)
        assert not within_hysteresis(
            cpu.model_copy(update={"current_cpu_request_millicores": 900, "recommended_cpu_request_millicores": 305}),
            stored,
        )
        assert REVISION_HYSTERESIS == 0.05

    def test_a_direction_change_is_never_held(self):
        [cpu, _] = _pair(990, 256 * MIB)
        stored = RecommendationRecord.from_recommendation(cpu)
        increase = cpu.model_copy(update={"recommended_cpu_request_millicores": 1010})

        assert not within_hysteresis(increase, stored)
        assert apply_revision_hysteresis([increase], [stored]) == [increase]


def _cpu_200(target: int) -> Recommendation:
    rec = _recommendation(recommendation_id=0, container_name="api", cpu_request=200, cpu_target=target)
    rec.id = None
    rec.description = f"cpu {target}m"
    rec.patch = RecommendationPatchV2.model_validate(build_patch(rec))
    return rec


async def _cpu_state(repo) -> tuple[int, int | None]:
    [cpu] = await repo.get_active_recommendations()
    return cpu.revision, cpu.recommended_cpu_request_millicores


class TestDeadband:
    """IMP-13: an absolute floor and a confirmation memory, so alternating proposals do not flip revisions."""

    async def test_proposals_alternating_within_the_floor_settle_on_the_higher_value(self, repo):
        engine = OptimizationEngine(sources=[])
        states = []
        for target in (56, 60, 56, 60, 56):
            await engine.persist([_cpu_200(target)], repo)
            states.append(await _cpu_state(repo))
        # The higher (safer) request is taken at once; the lower one is then within the 10m floor.
        assert states == [(1, 56), (2, 60), (2, 60), (2, 60), (2, 60)]

    async def test_proposals_alternating_from_the_higher_value_make_one_revision(self, repo):
        engine = OptimizationEngine(sources=[])
        for target in (60, 56, 60, 56, 60):
            await engine.persist([_cpu_200(target)], repo)
        assert await _cpu_state(repo) == (1, 60)

    async def test_a_lower_request_is_revised_at_the_third_consecutive_scan(self, repo):
        engine = OptimizationEngine(sources=[])
        states = []
        for target in (80, 56, 56, 56):
            await engine.persist([_cpu_200(target)], repo)
            states.append(await _cpu_state(repo))
        assert states == [(1, 80), (1, 80), (1, 80), (2, 56)]

    async def test_an_interrupted_confirmation_starts_again(self, repo):
        engine = OptimizationEngine(sources=[])
        for target in (80, 56, 56, 80, 56, 56):
            await engine.persist([_cpu_200(target)], repo)
        assert await _cpu_state(repo) == (1, 80)

    async def test_a_higher_request_is_accepted_at_once(self, repo):
        engine = OptimizationEngine(sources=[])
        for target in (56, 80):
            await engine.persist([_cpu_200(target)], repo)
        assert await _cpu_state(repo) == (2, 80)


async def test_a_frozen_record_drops_its_pending_confirmation(repo):
    """IMP-13 review: a confirmation count never survives a scan that kept a frozen record."""
    engine = OptimizationEngine(sources=[])
    await engine.persist([_cpu_200(80)], repo)
    [cpu] = await repo.get_active_recommendations()
    async with repo.db_manager.connection_scope() as conn:
        await conn.execute(
            "UPDATE recommendation_history SET pending_revision = ?, action_status = 'plan_ready' WHERE id = ?",
            ('{"value": 56, "scans": 2}', cpu.id),
        )
        await conn.commit()

    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(_cpu_200(80))])

    stored = await repo.get_recommendation_by_id(cpu.id)
    assert stored is not None and stored.pending_revision is None
