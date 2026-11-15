"""Measured released capacity and the capacity verdict (WP-08.1).

A request change is verified from the workload specs alone: the per-container
requests recorded with each spec observation give the per-replica requested
capacity before and after, the hour buckets give the replicas. The verdict
needs no price, no OpenCost and no autoscaler, and a replica change is reported
apart, never booked as a release. Runs on SQLite and, with
``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from datetime import timedelta

import pytest

from greenkube.core.optimization.applied_detector import WorkloadSnapshot
from greenkube.verify.model import MeasurementState, TimeWindow, WorkloadSpecObservation
from greenkube.verify.service import DECISION_GRACE
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import Scenario  # pyrefly: ignore[missing-import]

GIB = 1024**3


def _resources(cpu: int, memory: int) -> dict:
    return {
        "api": {
            "cpu_request_millicores": cpu,
            "memory_request_bytes": memory,
            "cpu_limit_millicores": None,
            "memory_limit_bytes": 2 * memory,
        }
    }


def _without_opencost(scenario: Scenario) -> None:
    """Rows priced with configured rates only: no trusted cost, so no cost verdict (MEA-06)."""
    for row in [*scenario.metrics.pod_rows, *scenario.metrics.container_rows]:
        row.cost_source = "custom_rate"


async def _observe(scenario: Scenario, before_at, after_at) -> None:
    await scenario.changes.insert_spec_observations(
        [
            WorkloadSpecObservation(
                namespace="prod",
                kind="Deployment",
                name="api",
                workload_uid="uid-api",
                generation=1,
                spec_digest="digest-1",
                container_resources=_resources(1000, 2 * GIB),
                observed_at=before_at,
            ),
            WorkloadSpecObservation(
                namespace="prod",
                kind="Deployment",
                name="api",
                workload_uid="uid-api",
                generation=2,
                spec_digest="digest-2",
                container_resources=_resources(300, GIB),
                observed_at=after_at,
            ),
        ]
    )


async def _decide(scenario: Scenario, change):
    completed = scenario.completed
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_start + timedelta(hours=72) + DECISION_GRACE)
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    status = await scenario.changes.get_status(change.change_id)
    measurements = await scenario.changes.list_measurements(change.change_id)
    assert len(measurements) == 1
    return status, measurements[0]


async def test_a_request_decrease_reaches_released_without_opencost_or_autoscaler(database):  # noqa: F811
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=GIB
    )
    _without_opencost(scenario)
    await _observe(scenario, completed - timedelta(days=9), completed - timedelta(minutes=5))
    change, _ = await scenario.apply()

    status, measurement = await _decide(scenario, change)
    # The cost verdict has no trusted price; the capacity verdict does not need one.
    assert status is not None and status.state is MeasurementState.INCONCLUSIVE
    capacity = measurement.capacity
    assert capacity is not None
    assert capacity["method"] == "measured"
    assert capacity["verdict"] == "released"
    assert capacity["per_replica"]["cpu_cores"] == pytest.approx(0.7)
    assert capacity["per_replica"]["mem_gib"] == pytest.approx(1.0)
    assert capacity["replicas"]["before"] == pytest.approx(2.0)
    assert capacity["replicas"]["after"] == pytest.approx(2.0)
    assert capacity["total"]["cpu_cores"] == pytest.approx(1.4)
    assert capacity["total"]["mem_gib"] == pytest.approx(2.0)
    hours = capacity["post_window_hours"]
    assert hours == pytest.approx(7 * 24)
    assert capacity["cpu_core_hours"] == pytest.approx(1.4 * hours)
    assert capacity["mem_gib_hours"] == pytest.approx(2.0 * hours)
    assert capacity["replica_change"] is None
    assert capacity["units"]["cpu_core_hours"] == "core-hours"

    # The API returns it with the measurement, labelled measured.
    stored = await scenario.changes.list_measurements(change.change_id)
    assert stored[0].capacity == capacity


async def test_a_replica_change_in_the_post_window_is_reported_apart(database):  # noqa: F811
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)),
        pods=("api-a", "api-b", "api-c"),
        cpu_request=300,
        memory_request=GIB,
    )
    _without_opencost(scenario)
    await _observe(scenario, completed - timedelta(days=9), completed - timedelta(minutes=5))
    change, _ = await scenario.apply()

    _, measurement = await _decide(scenario, change)
    capacity = measurement.capacity
    assert capacity is not None and capacity["verdict"] == "released"
    # The request effect is booked per replica at the baseline replica count ...
    assert capacity["per_replica"]["cpu_cores"] == pytest.approx(0.7)
    assert capacity["total"]["cpu_cores"] == pytest.approx(0.7 * 2)
    # ... so that request effect + replica effect = net (2 x 1000m before, 3 x 300m after).
    assert capacity["net"]["cpu_cores"] == pytest.approx(2 * 1.0 - 3 * 0.3)
    # ... and the replica change is reported separately, never as a release.
    change_block = capacity["replica_change"]
    assert change_block["before"] == pytest.approx(2.0) and change_block["after"] == pytest.approx(3.0)
    assert change_block["cpu_cores"] == pytest.approx(-0.3)  # one more replica at 300m reserves 0.3 core
    assert change_block["mem_gib"] == pytest.approx(-1.0)


async def test_no_request_change_is_not_released(database):  # noqa: F811
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed + timedelta(days=9)))
    _without_opencost(scenario)
    await scenario.changes.insert_spec_observations(
        [
            WorkloadSpecObservation(
                namespace="prod",
                kind="Deployment",
                name="api",
                generation=1,
                container_resources=_resources(1000, 2 * GIB),
                observed_at=completed - timedelta(days=9),
            )
        ]
    )
    change, _ = await scenario.apply()
    _, measurement = await _decide(scenario, change)
    assert measurement.capacity is not None and measurement.capacity["verdict"] == "not_released"


async def test_workload_observations_record_per_container_requests_and_limits(database):  # noqa: F811
    scenario = Scenario(database)
    snapshot = WorkloadSnapshot(
        namespace="prod",
        kind="Deployment",
        name="api",
        uid="uid-api",
        generation=3,
        spec_digest="digest-3",
        resources_digest="res-3",
        container_request_values={"api": (300, GIB)},
        container_limit_values={"api": (None, 2 * GIB)},
    )
    with_init = WorkloadSnapshot(
        namespace="prod",
        kind="Deployment",
        name="web",
        generation=1,
        container_request_values={"web": (300, GIB), "migrate": (2000, GIB), "proxy": (100, GIB)},
        container_kinds={"web": "container", "migrate": "init", "proxy": "sidecar"},
    )
    assert await scenario.service.observe_workloads({("prod", "Deployment", "api"): snapshot}) == 1
    latest = await scenario.changes.latest_spec_observations()
    assert latest[("prod", "Deployment", "api")].container_resources == _resources(300, GIB)
    # Same revision: nothing new is recorded.
    assert await scenario.service.observe_workloads({("prod", "Deployment", "api"): snapshot}) == 0
    # Container kinds are recorded so init containers stay out of the released capacity.
    await scenario.service.observe_workloads({("prod", "Deployment", "web"): with_init})
    web = (await scenario.changes.latest_spec_observations())[("prod", "Deployment", "web")]
    assert web.container_resources is not None
    assert web.container_resources["migrate"]["kind"] == "init"
    assert web.container_resources["proxy"]["kind"] == "sidecar"


def test_the_workload_reader_parses_container_limits():
    from greenkube.core.optimization.applied_detector import _read_container_request_values

    containers = [{"name": "api", "resources": {"requests": {"cpu": "300m"}, "limits": {"memory": "2Gi"}}}]
    limits, valid = _read_container_request_values(containers, "limits")
    assert valid and limits == {"api": (None, 2 * GIB)}
    requests, _ = _read_container_request_values(containers)
    assert requests == {"api": (300, None)}


def test_the_workload_reader_names_init_containers_and_native_sidecars():
    from greenkube.core.optimization.applied_detector import _container_kinds

    kinds = _container_kinds(
        [{"name": "api"}],
        [
            {"name": "migrate"},
            {"name": "proxy", "restart_policy": "Always"},
            {"name": "mesh", "restartPolicy": "Always"},
        ],
    )
    assert kinds == {"api": "container", "migrate": "init", "proxy": "sidecar", "mesh": "sidecar"}
