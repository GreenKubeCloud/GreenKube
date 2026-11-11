"""Persisted controller loop heartbeats on SQLite and PostgreSQL (M8, migration 0076)."""

from datetime import datetime, timedelta, timezone

from greenkube.core.loops import LoopHeartbeatPublisher, LoopRegistry, LoopSpec
from greenkube.storage.controller_heartbeat_repository import ControllerHeartbeat, ControllerHeartbeatRepository
from tests.storage.test_workload_usage_repository import db  # noqa: F401  # pyrefly: ignore[missing-import]


async def test_publish_upserts_and_lists_recent_heartbeats(db):  # noqa: F811
    repository = ControllerHeartbeatRepository(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    loops = [{"name": "collection", "state": "idle", "ready": True, "consecutive_failures": 0}]
    await repository.publish(
        ControllerHeartbeat("controller-0", "controller", True, True, now - timedelta(minutes=1), now, loops)
    )
    await repository.publish(
        ControllerHeartbeat("controller-0", "controller", True, False, now, now - timedelta(hours=1), loops)
    )
    await repository.publish(
        ControllerHeartbeat("controller-old", "controller", False, True, now - timedelta(days=2), now, [])
    )

    recent = await repository.list_since(now - timedelta(hours=1))
    assert [item.instance_id for item in recent] == ["controller-0"]
    (row,) = recent
    assert row.leader is True and row.ready is False
    assert row.heartbeat_at == now and row.started_at == now - timedelta(hours=1)
    assert row.loops == loops

    await repository.prune_before(now - timedelta(days=1))
    assert [item.instance_id for item in await repository.list_since(now - timedelta(days=3))] == ["controller-0"]


async def test_the_publisher_persists_the_registry_snapshot(db):  # noqa: F811
    repository = ControllerHeartbeatRepository(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    registry = LoopRegistry(role="controller", leader=lambda: True, clock=lambda: now)

    async def failing() -> None:
        raise RuntimeError("boom")

    registry.register(LoopSpec("automation-controller", failing, 60))
    await registry.run_once("automation-controller")
    publisher = LoopHeartbeatPublisher(registry, repository, instance_id="controller-1", clock=lambda: now)
    registry.register(publisher.spec())
    await publisher.publish()

    (row,) = await repository.list_since(now - timedelta(minutes=1))
    assert row.instance_id == "controller-1" and row.leader is True
    loop = {item["name"]: item for item in row.loops}["automation-controller"]
    assert loop["state"] == "failing"
    assert loop["consecutive_failures"] == 1 and loop["last_error_code"] == "RuntimeError"
    assert publisher.spec().leader_only is False
