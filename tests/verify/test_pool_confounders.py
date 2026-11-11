"""Pool-level confounders (C4): node pool hardware, autoscaling and changes to neighbours."""

from datetime import timedelta

from greenkube.verify.confounders import (
    ConfounderDetector,
    PoolContext,
    PoolNode,
    PoolSample,
    confidence_score,
    is_contaminated,
)
from greenkube.verify.model import WorkloadRef
from greenkube.verify.strategy import MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event, hour_now  # pyrefly: ignore[missing-import]


def _setup():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed)
    plan = RequestRightsizingStrategy().required_windows(change, MeasurementSettings())
    return change, plan


def _samples(start, end, nodes_at):
    samples = []
    moment = start
    while moment < end:
        samples.append(PoolSample(at=moment, nodes=tuple(nodes_at(moment))))
        moment += timedelta(hours=6)
    return tuple(samples)


def _node(name, instance_type="m5.large", cores=2.0):
    return PoolNode(name=name, pool="general", instance_type=instance_type, cpu_cores=cores)


def _steady(_):
    return [_node("n1"), _node("n2"), _node("n3")]


def _detect(change, plan, pre_nodes=_steady, post_nodes=_steady, others=()):
    pool = PoolContext(
        pools=("general",),
        pre=_samples(plan.pre.start, plan.pre.end, pre_nodes),
        post=_samples(plan.post.start, plan.post.end, post_nodes),
        other_changes=tuple(others),
    )
    return ConfounderDetector().detect(change, plan, pool=pool)


def test_a_steady_pool_has_no_confounders():
    change, plan = _setup()
    assert _detect(change, plan) == []


def test_new_hardware_in_the_pool_contaminates_the_measurement():
    change, plan = _setup()
    findings = _detect(
        change, plan, post_nodes=lambda _: [_node("g1", "c6g.large"), _node("g2", "c6g.large"), _node("n3")]
    )
    kinds = {(item.kind, item.severity) for item in findings}
    assert ("pool_instance_type_change", "severe") in kinds
    assert is_contaminated(findings)
    finding = next(item for item in findings if item.kind == "pool_instance_type_change")
    assert "general" in finding.detail and finding.evidence["after"]["c6g.large"] > 0.6


def test_autoscaler_scale_up_lowers_confidence():
    change, plan = _setup()
    findings = _detect(change, plan, post_nodes=lambda _: [_node("n1"), _node("n2"), _node("n3"), _node("n4")])
    assert [(item.kind, item.severity) for item in findings] == [("pool_capacity_change", "moderate")]
    assert findings[0].evidence["before_cores"] == 6.0 and findings[0].evidence["after_cores"] == 8.0
    assert not is_contaminated(findings)
    assert confidence_score(findings, 1.0, None) < 1.0 - 0.10


def test_pool_halved_by_the_autoscaler_contaminates_the_measurement():
    change, plan = _setup()
    findings = _detect(
        change,
        plan,
        pre_nodes=lambda _: [_node(f"n{i}") for i in range(6)],
        post_nodes=lambda _: [_node("n0"), _node("n1")],
    )
    assert [(item.kind, item.severity) for item in findings] == [("pool_capacity_change", "severe")]
    assert is_contaminated(findings)


def test_node_churn_without_a_capacity_shift_is_mild():
    change, plan = _setup()
    midpoint = plan.post.start + timedelta(days=3)

    def rotated(moment):
        # n3 is replaced by n4 halfway through the post-window (same capacity).
        return [_node("n1"), _node("n2"), _node("n3" if moment < midpoint else "n4")]

    findings = _detect(change, plan, post_nodes=rotated)
    assert [(item.kind, item.severity) for item in findings] == [("autoscaler_activity", "mild")]
    assert findings[0].evidence == {"added": ["n4"], "removed": ["n3"]}


def test_a_change_to_a_neighbour_in_the_pool_lowers_confidence():
    change, plan = _setup()
    neighbour = change_event(change.rollout_completed_at + timedelta(days=1), change_id="change-web").model_copy(
        update={"target": WorkloadRef(namespace="prod", kind="Deployment", name="web")}
    )
    same_workload = change_event(change.rollout_completed_at + timedelta(days=1), change_id="change-api-2")
    findings = _detect(change, plan, others=[change, neighbour, same_workload])
    assert [(item.kind, item.severity) for item in findings] == [("pool_change", "moderate")]
    assert findings[0].evidence["change_id"] == "change-web"
    assert not is_contaminated(findings)
