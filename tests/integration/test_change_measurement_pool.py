"""Pool-level confounders of change measurement on real databases (C4).

The workload's node pool is read from the node snapshots (SCD2) over both
windows: new hardware in the pool contaminates the measurement, a change to
another workload running in the pool only lowers its confidence. Runs on
SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from datetime import datetime, timedelta

from greenkube.models.node import NodeInfo
from greenkube.storage.postgres.node_repository import PostgresNodeRepository
from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository
from greenkube.verify.model import ChangeOutcome, MeasurementState, TimeWindow, WorkloadRef
from greenkube.verify.service import DECISION_GRACE
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import Scenario, change_event, workload_rows  # pyrefly: ignore[missing-import]


def _node(name: str, instance_type: str, at: datetime, pool: str = "general") -> NodeInfo:
    return NodeInfo(
        name=name,
        instance_type=instance_type,
        node_pool=pool,
        cpu_capacity_cores=2.0,
        memory_capacity_bytes=8 * 1024**3,
        timestamp=at,
    )


def _node_repo(manager):
    return PostgresNodeRepository(manager) if manager.db_type == "postgres" else SQLiteNodeRepository(manager)


async def _scenario(manager) -> Scenario:
    scenario = Scenario(manager)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    # The api pods ran on nodes n1 and n2 of the "general" pool.
    scenario.metrics.pod_rows = [
        row.model_copy(update={"node": "n1" if row.pod_name.endswith("a") else "n2"})
        for row in scenario.metrics.pod_rows
    ]
    nodes = _node_repo(manager)
    start = completed - timedelta(days=12)
    await nodes.save_nodes([_node("n1", "m5.large", start), _node("n2", "m5.large", start)])
    scenario.service.node_repo = nodes
    return scenario


async def _decide(scenario: Scenario, change_id: str):
    await scenario.service.run_due(now=scenario.completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    status = await scenario.changes.get_status(change_id)
    measurement = (await scenario.changes.list_measurements(change_id))[0]
    return status, measurement


async def test_new_hardware_in_the_node_pool_contaminates_the_measurement(database):  # noqa: F811
    scenario = await _scenario(database)
    change, _ = await scenario.apply()
    # One day into the post-window the pool's nodes are replaced by Graviton instances.
    swap = scenario.completed + timedelta(days=1)
    await scenario.service.node_repo.save_nodes([_node("n1", "c6g.large", swap), _node("n2", "c6g.large", swap)])

    status, measurement = await _decide(scenario, change.change_id)

    assert status is not None and status.state is MeasurementState.CONTAMINATED
    assert measurement.outcome is ChangeOutcome.CONTAMINATED
    kinds = {(item.kind, item.severity) for item in measurement.confounders}
    assert ("pool_instance_type_change", "severe") in kinds
    assert any("node pool general changed hardware" in reason for reason in measurement.reasons)


async def test_a_neighbour_change_in_the_pool_lowers_confidence_only(database):  # noqa: F811
    scenario = await _scenario(database)
    change, _ = await scenario.apply()
    landed = scenario.completed + timedelta(days=2)
    neighbour = change_event(landed, change_id="change-web", recommendation_ids=[]).model_copy(
        update={"target": WorkloadRef(namespace="prod", kind="Deployment", name="web"), "workload_uid": "uid-web"}
    )
    await scenario.changes.insert_change(neighbour)
    # The web pods run on n1, in the same pool.
    web = workload_rows(TimeWindow(start=landed, end=landed + timedelta(hours=3)), pods=("web-a",))
    scenario.metrics.pod_rows.extend(row.model_copy(update={"owner_name": "web", "node": "n1"}) for row in web)

    status, measurement = await _decide(scenario, change.change_id)

    assert status is not None and status.state is MeasurementState.REALIZED
    assert [(item.kind, item.severity) for item in measurement.confounders] == [("pool_change", "moderate")]
    assert measurement.confounders[0].evidence["change_id"] == "change-web"
    assert 0 < measurement.confidence < 1.0


async def test_a_steady_pool_adds_no_confounder(database):  # noqa: F811
    scenario = await _scenario(database)
    change, _ = await scenario.apply()
    status, measurement = await _decide(scenario, change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    assert measurement.confounders == []
