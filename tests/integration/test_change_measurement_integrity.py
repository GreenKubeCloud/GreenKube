"""Measurement integrity on real databases (production readiness): net component split, replicas.

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from datetime import timedelta

import pytest

from greenkube.verify.model import MeasurementState, TimeWindow
from greenkube.verify.service import DECISION_GRACE
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import Scenario, record_status  # pyrefly: ignore[missing-import]


async def _decide(scenario: Scenario):
    change, applied = await scenario.apply()
    await scenario.service.run_due(now=scenario.completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    return change, applied


async def test_a_cpu_cut_with_a_memory_increase_books_the_net_saving(database):  # noqa: F811
    """CPU 1000m -> 300m saves 2 x 0.7 x $0.08/h; memory 2 -> 3 GiB costs 2 x $0.01/h: the change books the net."""
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=3 * 1024**3
    )
    change, applied = await _decide(scenario)
    cpu_id, memory_id = (record.id for record in applied)
    assert (await scenario.changes.get_status(change.change_id)).state is MeasurementState.REALIZED
    measurement = (await scenario.changes.list_measurements(change.change_id))[0]
    net = measurement.estimate("allocated_cost")
    gross_cpu = measurement.estimate("cost_cpu")
    assert net is not None and gross_cpu is not None and net.annualized is not None
    assert gross_cpu.annualized is not None and gross_cpu.annualized > net.annualized > 0

    records = {record.id: record for record in await scenario.records()}
    assert record_status(records[cpu_id]) == "verified"
    # The CPU recommendation claims the change's net saving, not the gross CPU saving.
    assert records[cpu_id].measured_cost_saved == pytest.approx(net.annualized, rel=1e-6)
    assert records[cpu_id].measured_cost_saved == pytest.approx(2 * (0.7 * 0.08 - 0.01) * 8766, rel=0.02)
    claimed = sum(
        float(records[rec_id].measured_cost_saved or 0.0)
        for rec_id in (cpu_id, memory_id)
        if record_status(records[rec_id]) == "verified"
    )
    assert claimed <= net.annualized * (1 + 1e-5)  # shares are stored rounded to 6 decimals


def _projection(allocated: float, realization: float) -> dict:
    return {
        "model_version": "2.0.0",
        "allocated_cost_annual": allocated,
        "expected_billable_cost_annual": allocated * realization,
        "realization_factor": realization,
        "co2e_modeled": False,
    }


async def test_projection_accuracy_compares_the_measurement_with_the_allocated_projection(database):  # noqa: F811
    """No autoscaler can remove nodes (realization 0.25): the billable projection is a quarter of the allocated one.

    The measurement reads allocated cost, so it is compared with the allocated projection (accuracy 1.0), not
    with the billable one (accuracy 4.0 before the fix).
    """
    from greenkube.models.metrics import RecommendationType

    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    cpu_allocated = 2 * 0.7 * 0.08 * 8766
    memory_allocated = 2 * 0.01 * 8766
    fields = {
        RecommendationType.RIGHTSIZING_CPU: {
            "impact": _projection(cpu_allocated, 0.25),
            "potential_savings_cost": cpu_allocated * 0.25,
        },
        RecommendationType.RIGHTSIZING_MEMORY: {
            "impact": _projection(memory_allocated, 0.25),
            "potential_savings_cost": memory_allocated * 0.25,
        },
    }
    change, applied = await scenario.apply(fields=fields)
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)

    measurement = (await scenario.changes.list_measurements(change.change_id))[0]
    cost = measurement.estimate("allocated_cost")
    assert cost is not None and cost.projected_annual == pytest.approx(cpu_allocated + memory_allocated)
    assert cost.projection_accuracy == pytest.approx(1.0, rel=0.02)
    rows = {row.recommendation_id: row for row in await scenario.measurements.list_for_change(change.change_id)}
    cpu_cost = next(item for item in rows[applied[0].id].estimates if item.quantity == "cost")
    assert cpu_cost.projected_annual == pytest.approx(cpu_allocated)
    assert cpu_cost.projection_accuracy == pytest.approx(1.0, rel=0.02)
