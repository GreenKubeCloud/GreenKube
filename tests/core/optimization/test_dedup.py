# tests/core/optimization/test_dedup.py
"""Tests for cross-source arbitration and provenance."""

from greenkube.core.config import Config
from greenkube.core.optimization.dedup import finalize_recommendations
from greenkube.models.metrics import (
    Recommendation,
    RecommendationCapability,
    RecommendationSource,
    RecommendationType,
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


def test_vpa_wins_over_native_for_same_capability():
    native = _workload_rec(source=RecommendationSource.GREENKUBE, cpu=250)
    vpa = _workload_rec(source=RecommendationSource.VPA, cpu=200)

    recs = finalize_recommendations([native, vpa], Config())

    assert len(recs) == 1
    assert recs[0].source == RecommendationSource.VPA
    assert recs[0].sources == ["vpa", "greenkube"]
    assert recs[0].recommended_cpu_request_millicores == 200


def test_priority_config_can_flip_winner():
    cfg = Config()
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
