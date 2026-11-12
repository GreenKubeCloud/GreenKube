"""Pool-wide price-card control (C4 remainder): unit prices of the node pool as a confounder check."""

from datetime import timedelta

import pytest

from greenkube.verify.confounders import (
    ConfounderDetector,
    PoolContext,
    PoolNode,
    PoolSample,
    is_contaminated,
    pool_unit_prices,
)
from greenkube.verify.model import Confounder, TimeWindow
from greenkube.verify.strategy import MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event, hour_now, workload_rows  # pyrefly: ignore[missing-import]


def _setup():
    change = change_event(hour_now() - timedelta(days=9))
    return change, RequestRightsizingStrategy().required_windows(change, MeasurementSettings())


def _pool(plan, before, after) -> PoolContext:
    nodes = (PoolNode(name="n1", pool="general", instance_type="m5.large", cpu_cores=2.0),)
    return PoolContext(
        pools=("general",),
        pre=(PoolSample(at=plan.pre.start, nodes=nodes),),
        post=(PoolSample(at=plan.post.start, nodes=nodes),),
        prices_pre=before,
        prices_post=after,
    )


def test_a_pool_price_shift_is_flagged_by_resource_and_severity():
    change, plan = _setup()
    detector = ConfounderDetector()
    moderate = detector.detect(
        change, plan, pool=_pool(plan, {"cpu": 0.08, "memory": 0.01}, {"cpu": 0.088, "memory": 0.0104})
    )
    assert [(item.kind, item.evidence["resource"], item.severity) for item in moderate] == [
        ("pool_price_card_change", "cpu", "moderate")
    ]
    severe = detector.detect(change, plan, pool=_pool(plan, {"cpu": 0.08}, {"cpu": 0.06}))
    assert is_contaminated(severe) and "-25%" in severe[0].detail
    assert detector.detect(change, plan, pool=_pool(plan, {"cpu": 0.08}, {})) == []


def test_the_pool_finding_replaces_the_workload_finding_of_the_same_resource(monkeypatch):
    change, plan = _setup()
    workload_level = Confounder(
        kind="price_card_change", severity="moderate", detail="cpu", evidence={"resource": "cpu"}
    )
    monkeypatch.setattr(ConfounderDetector, "_price_change", staticmethod(lambda pre, post: [workload_level]))
    detector = ConfounderDetector()
    findings = detector.detect(change, plan, pool=_pool(plan, {"cpu": 0.08}, {"cpu": 0.1}))
    assert [item.kind for item in findings] == ["pool_price_card_change"]
    steady = detector.detect(change, plan, pool=_pool(plan, {"cpu": 0.08}, {"cpu": 0.08}))
    assert [item.kind for item in steady] == ["price_card_change"]


def test_pool_unit_prices_use_trusted_rows_on_the_pool_nodes_only():
    _, plan = _setup()
    window = TimeWindow(start=plan.pre.start, end=plan.pre.start + timedelta(hours=4))
    inside = [row.model_copy(update={"node": "n1"}) for row in workload_rows(window, cpu_price=0.08, ram_price=0.01)]
    outside = [row.model_copy(update={"node": "other"}) for row in workload_rows(window, cpu_price=0.5)]
    untrusted = [
        row.model_copy(update={"node": "n1"}) for row in workload_rows(window, cpu_price=0.9, is_estimated=True)
    ]
    prices = pool_unit_prices(inside + outside + untrusted, {"n1"}, 300)
    assert prices["cpu"] == pytest.approx(0.08)
    assert prices["memory"] == pytest.approx(0.01)
    assert pool_unit_prices(outside, {"n1"}, 300) == {}
