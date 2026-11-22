"""Pool-wide price-card control of change measurement (roadmap C4 remainder).

A rightsizing that resizes both CPU and memory leaves no untouched resource of
the workload to control the unit prices with. The node pool's price card (the
unit prices of every pod on the pool's nodes) is that control: a price rise
during the measurement would otherwise be read as a smaller saving, a price
cut as a larger one. Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``,
on PostgreSQL.
"""

from datetime import timedelta

import pytest

from greenkube.verify.model import ChangeOutcome, MeasurementState, TimeWindow
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.integration.test_change_measurement_pool import _decide, _node, _node_repo  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import Scenario, workload_rows  # pyrefly: ignore[missing-import]


async def _scenario(manager, *, post_price_factor: float) -> Scenario:
    scenario = Scenario(manager)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)),
        cpu_request=300,
        memory_request=1024**3,
        cpu_price=0.08 * post_price_factor,
        ram_price=0.01 * post_price_factor,
    )
    # Another workload of the pool, never changed, pays the same price card.
    web = workload_rows(TimeWindow(start=completed - timedelta(days=8), end=completed), pods=("web-a",))
    web += workload_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)),
        pods=("web-a",),
        cpu_price=0.08 * post_price_factor,
        ram_price=0.01 * post_price_factor,
    )
    scenario.metrics.pod_rows = [
        row.model_copy(update={"node": "n1" if row.pod_name.endswith("a") else "n2"})
        for row in scenario.metrics.pod_rows
    ] + [row.model_copy(update={"owner_name": "web", "node": "n1"}) for row in web]
    nodes = _node_repo(manager)
    await nodes.save_nodes(
        [
            _node("n1", "m5.large", completed - timedelta(days=12)),
            _node("n2", "m5.large", completed - timedelta(days=12)),
        ]
    )
    scenario.service.node_repo = nodes
    return scenario


async def test_a_pool_wide_price_rise_contaminates_a_two_resource_rightsizing(database):  # noqa: F811
    scenario = await _scenario(database, post_price_factor=1.3)
    change, _ = await scenario.apply()

    status, measurement = await _decide(scenario, change.change_id)

    assert status is not None and status.state is MeasurementState.CONTAMINATED
    assert measurement.outcome is ChangeOutcome.CONTAMINATED
    findings = {(item.kind, item.evidence.get("resource"), item.severity) for item in measurement.confounders}
    assert ("pool_price_card_change", "cpu", "severe") in findings
    assert ("pool_price_card_change", "memory", "severe") in findings
    cpu = next(
        item
        for item in measurement.confounders
        if item.kind == "pool_price_card_change" and item.evidence["resource"] == "cpu"
    )
    assert cpu.evidence["after"] / cpu.evidence["before"] == pytest.approx(1.3, rel=1e-6)
    # The workload-level check could not run: both of its resources were resized.
    assert not [item for item in measurement.confounders if item.kind == "price_card_change"]


async def test_a_moderate_pool_price_change_lowers_confidence_only(database):  # noqa: F811
    scenario = await _scenario(database, post_price_factor=1.1)
    change, _ = await scenario.apply()

    status, measurement = await _decide(scenario, change.change_id)

    assert status is not None and status.state is MeasurementState.REALIZED
    kinds = sorted((item.kind, item.severity) for item in measurement.confounders)
    assert kinds == [("pool_price_card_change", "moderate"), ("pool_price_card_change", "moderate")]
    assert 0 < measurement.confidence < 1.0


async def test_a_steady_price_card_adds_nothing(database):  # noqa: F811
    scenario = await _scenario(database, post_price_factor=1.0)
    change, _ = await scenario.apply()
    status, measurement = await _decide(scenario, change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    assert measurement.confounders == []
