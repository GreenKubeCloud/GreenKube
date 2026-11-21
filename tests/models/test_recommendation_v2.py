"""Recommendation v2 model: identity, round trips and revisions (roadmap E4, E7)."""

from __future__ import annotations

import pytest

from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationSource, RecommendationType
from greenkube.models.recommendation_v2 import (
    FLAT_FIELDS,
    NewerRevision,
    advice_signature,
    compute_fingerprint_v4,
    flat_fields,
    to_v2,
    with_v2_fields,
)
from greenkube.signals.model import TargetRef

CLUSTER = "prod-eu"
MIB = 1024 * 1024


def _v1_recommendations() -> list[Recommendation]:
    workload = dict(namespace="shop", scope="workload", owner_kind="Deployment", owner_name="api", pod_name="api")
    return [
        Recommendation(
            type=RecommendationType.RIGHTSIZING_CPU,
            container_name="web",
            description="cpu",
            reason="low usage",
            current_cpu_request_millicores=500,
            recommended_cpu_request_millicores=300,
            source=RecommendationSource.VPA,
            source_ref="shop/api-vpa",
            **workload,
        ),
        Recommendation(
            type=RecommendationType.RIGHTSIZING_MEMORY,
            container_name="web",
            description="memory",
            current_memory_request_bytes=1024 * MIB,
            recommended_memory_request_bytes=512 * MIB,
            **workload,
        ),
        Recommendation(type=RecommendationType.AUTOSCALING_CANDIDATE, description="hpa", priority="low", **workload),
        Recommendation(
            type=RecommendationType.OFF_PEAK_SCALING,
            description="night",
            cron_schedule="Scale to 0: 20:00-07:00 Europe/Paris",
            **workload,
        ),
        Recommendation(type=RecommendationType.ZOMBIE_POD, description="zombie", **workload),
        Recommendation(type=RecommendationType.IDLE_NAMESPACE, namespace="old", scope="namespace", description="idle"),
        Recommendation(
            type=RecommendationType.ORPHANED_PERSISTENT_VOLUME, pod_name="pv-1", scope="cluster", description="pv"
        ),
        Recommendation(
            type=RecommendationType.ORPHANED_LOAD_BALANCER,
            pod_name="lb",
            namespace="shop",
            scope="cluster",
            description="lb",
        ),
        Recommendation(
            type=RecommendationType.OVERPROVISIONED_NODE,
            target_node="nodepool/default",
            scope="node",
            description="pool",
            source="karpenter",
        ),
        Recommendation(type=RecommendationType.UNDERUTILIZED_NODE, target_node="node-1", scope="node", description="n"),
        Recommendation(type=RecommendationType.CARBON_AWARE_SCHEDULING, description="carbon", **workload),
        Recommendation(
            type=RecommendationType.RIGHTSIZING_CPU,
            pod_name="standalone",
            namespace="shop",
            scope="pod",
            description="bare pod",
            current_cpu_request_millicores=200,
            recommended_cpu_request_millicores=100,
        ),
    ]


@pytest.mark.parametrize("rec", _v1_recommendations(), ids=lambda rec: f"{rec.type.value}-{rec.scope}")
def test_v1_to_v2_to_v1_round_trip(rec: Recommendation):
    record = with_v2_fields(RecommendationRecord.from_recommendation(rec), CLUSTER)
    v1 = record.model_dump(mode="json", include=set(FLAT_FIELDS))

    v2 = to_v2(record, CLUSTER)

    assert v2.fingerprint == record.fingerprint_v4
    flat = flat_fields(v2)
    assert {name: flat.get(name) for name in FLAT_FIELDS} == v1
    # The JSON form of v2 round-trips too (what API clients see).
    reparsed = type(v2).model_validate_json(v2.model_dump_json())
    assert flat_fields(reparsed) == flat


def test_fingerprint_v4_is_cluster_qualified_and_stable():
    target = TargetRef.for_kind(CLUSTER, "Deployment", "api", namespace="shop", container="web")
    first = compute_fingerprint_v4(target, "container_requests", "RIGHTSIZING_CPU")
    assert first == compute_fingerprint_v4(target, "container_requests", "RIGHTSIZING_CPU")
    other_cluster = TargetRef.for_kind("other", "Deployment", "api", namespace="shop", container="web")
    assert compute_fingerprint_v4(other_cluster, "container_requests", "RIGHTSIZING_CPU") != first
    # CPU and memory records of one container stay distinct identities.
    assert compute_fingerprint_v4(target, "container_requests", "RIGHTSIZING_MEMORY") != first


def test_fingerprint_v4_does_not_depend_on_the_source_or_the_value():
    [cpu, *_] = _v1_recommendations()
    native = cpu.model_copy(
        update={"source": RecommendationSource.GREENKUBE, "recommended_cpu_request_millicores": 280}
    )
    first = with_v2_fields(RecommendationRecord.from_recommendation(cpu), CLUSTER).fingerprint_v4
    second = with_v2_fields(RecommendationRecord.from_recommendation(native), CLUSTER).fingerprint_v4
    assert first == second


def test_advice_signature_ignores_observed_state_and_projections():
    [cpu, *_] = _v1_recommendations()
    drifted = cpu.model_copy(
        update={"current_cpu_request_millicores": 520, "potential_savings_cost": 99.0, "description": "new words"}
    )
    changed = cpu.model_copy(update={"recommended_cpu_request_millicores": 250})
    assert advice_signature(cpu) == advice_signature(drifted)
    assert advice_signature(cpu) != advice_signature(changed)


def test_signal_only_source_ids_are_kept():
    rec = Recommendation(type=RecommendationType.ZOMBIE_POD, description="z", source="krr")
    assert rec.source == "krr"
    assert RecommendationRecord.from_recommendation(rec).source == "krr"
    # Anything that is not a source id falls back to greenkube.
    assert Recommendation(type=RecommendationType.ZOMBIE_POD, description="z", source="Not a source!").source == (
        RecommendationSource.GREENKUBE
    )


def test_newer_revision_note():
    note = NewerRevision(
        plan_id="p1",
        recommendation_id=12,
        pinned_revision=2,
        latest_revision=3,
        queued=True,
        snapshot={"recommended_cpu_request_millicores": 250},
    )
    assert note.describe() == (
        "recommendation #12 is at revision 3 (this change implements revision 2): CPU request 250m."
    )
