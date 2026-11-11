# tests/core/optimization/test_dedup.py
"""Tests for cross-source arbitration and provenance."""

from greenkube.core.config import Config
from greenkube.core.optimization.dedup import (
    SignalEntry,
    entries_for_recommendations,
    finalize_entries,
    finalize_recommendations,
)
from greenkube.models.metrics import (
    Recommendation,
    RecommendationCapability,
    RecommendationSource,
    RecommendationType,
)
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    ResourceSpec,
    SourceSignal,
    TargetRef,
)


def _workload_rec(source=RecommendationSource.GREENKUBE, rec_type=RecommendationType.RIGHTSIZING_CPU, cpu=200):
    return Recommendation(
        pod_name="api",
        namespace="prod",
        type=rec_type,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="desc",
        source=source,
        recommended_cpu_request_millicores=cpu,
    )


def test_single_source_gets_provenance():
    recs = finalize_recommendations([_workload_rec()], Config())
    assert len(recs) == 1
    assert recs[0].sources == ["greenkube"]


def test_safest_consensus_keeps_the_least_aggressive_proposal():
    native = _workload_rec(source=RecommendationSource.GREENKUBE, cpu=250)
    vpa = _workload_rec(source=RecommendationSource.VPA, cpu=200)

    recs = finalize_recommendations([native, vpa], Config())

    assert len(recs) == 1
    assert recs[0].source == RecommendationSource.GREENKUBE
    # No source's proposal is dropped unseen: both support the recommendation.
    assert recs[0].sources == ["greenkube", "vpa"]
    assert recs[0].recommended_cpu_request_millicores == 250
    consensus = recs[0].consensus or {}
    assert consensus["policy"] == "safest"
    assert consensus["spread"]["cpu_request_m"] == {"greenkube": 250, "vpa": 200}
    assert consensus["agreement"] == 0.8


def test_priority_policy_keeps_the_former_arbitration():
    cfg = Config()
    cfg.RECOMMENDATION_CONSENSUS_POLICY = "priority"
    native = _workload_rec(source=RecommendationSource.GREENKUBE, cpu=250)
    vpa = _workload_rec(source=RecommendationSource.VPA, cpu=200)

    recs = finalize_recommendations([native, vpa], cfg)

    assert len(recs) == 1
    assert recs[0].source == RecommendationSource.VPA
    assert recs[0].sources == ["vpa", "greenkube"]
    assert recs[0].recommended_cpu_request_millicores == 200


def test_source_priority_breaks_ties_between_equal_proposals():
    native = _workload_rec(source=RecommendationSource.GREENKUBE, cpu=200)
    vpa = _workload_rec(source=RecommendationSource.VPA, cpu=200)

    recs = finalize_recommendations([native, vpa], Config())

    assert len(recs) == 1
    assert recs[0].source == RecommendationSource.VPA
    assert (recs[0].consensus or {})["chosen_from"] == ["greenkube", "vpa"]


def test_priority_config_can_flip_winner():
    cfg = Config()
    cfg.RECOMMENDATION_CONSENSUS_POLICY = "priority"
    cfg.RECOMMENDATION_SOURCE_PRIORITY = "greenkube,vpa"
    native = _workload_rec(source=RecommendationSource.GREENKUBE, cpu=250)
    vpa = _workload_rec(source=RecommendationSource.VPA, cpu=200)

    recs = finalize_recommendations([native, vpa], cfg)

    assert len(recs) == 1
    assert recs[0].source == RecommendationSource.GREENKUBE


def test_distinct_capabilities_coexist():
    cpu = _workload_rec(rec_type=RecommendationType.RIGHTSIZING_CPU)
    mem = _workload_rec(rec_type=RecommendationType.RIGHTSIZING_MEMORY, cpu=None)
    mem = mem.model_copy(update={"recommended_memory_request_bytes": 1024})

    recs = finalize_recommendations([cpu, mem], Config())

    assert {r.type for r in recs} == {RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY}


def test_node_recommendations_do_not_collide():
    over = Recommendation(
        type=RecommendationType.OVERPROVISIONED_NODE,
        scope="node",
        target_node="node-1",
        description="over",
    )
    under = Recommendation(
        type=RecommendationType.UNDERUTILIZED_NODE,
        scope="node",
        target_node="node-1",
        description="under",
    )

    recs = finalize_recommendations([over, under], Config())

    assert {r.type for r in recs} == {
        RecommendationType.OVERPROVISIONED_NODE,
        RecommendationType.UNDERUTILIZED_NODE,
    }


def test_capability_is_derived_from_type():
    recs = finalize_recommendations([_workload_rec()], Config())
    assert recs[0].capability == RecommendationCapability.CPU_RIGHTSIZING


def _krr_entry(cpu: int, current: int = 500) -> SignalEntry:
    signal = SourceSignal(
        source_id="krr",
        target=TargetRef.for_kind("default", "Deployment", "api", namespace="prod"),
        capability=Capability.CONTAINER_REQUESTS,
        kind="RIGHTSIZING_CPU",
        proposal=ContainerResourcesProposal(
            current=ResourceSpec(cpu_request_m=current), proposed=ResourceSpec(cpu_request_m=cpu)
        ),
        algorithm=AlgorithmInfo(name="krr.simple"),
    )
    return (signal, "krr", None)


def test_a_consensus_that_keeps_the_current_request_is_no_recommendation():
    """KRR reports a value for every container: 'keep 500m' wins under safest, so nothing changes."""
    native = _workload_rec(cpu=280).model_copy(update={"current_cpu_request_millicores": 500})
    entries = entries_for_recommendations([native]) + [_krr_entry(500)]

    assert finalize_entries(entries, Config()) == []


def test_a_signal_only_source_alone_at_the_current_request_is_no_recommendation():
    assert finalize_entries([_krr_entry(500)], Config()) == []


def test_a_signal_only_source_below_the_current_request_still_counts():
    native = _workload_rec(cpu=280).model_copy(update={"current_cpu_request_millicores": 500})
    entries = entries_for_recommendations([native]) + [_krr_entry(300)]

    [rec] = finalize_entries(entries, Config())

    assert (rec.recommended_cpu_request_millicores, rec.current_cpu_request_millicores) == (300, 500)


def test_a_floored_value_equal_to_the_current_request_is_no_recommendation():
    cfg = Config()
    floor = cfg.RECOMMENDATION_MIN_CPU_MILLICORES
    native = _workload_rec(cpu=max(1, floor - 5)).model_copy(update={"current_cpu_request_millicores": floor})

    assert finalize_recommendations([native], cfg) == []
