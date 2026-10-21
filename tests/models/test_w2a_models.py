from greenkube.models.metrics import (
    Recommendation,
    RecommendationBaseline,
    RecommendationEvent,
    RecommendationEventPayload,
    RecommendationPatchV2,
    RecommendationRecord,
    RecommendationType,
)


def test_fingerprint_v3_is_stable_container_aware_and_owner_scoped():
    common = {
        "pod_name": "pod-a",
        "namespace": "default",
        "owner_kind": "Deployment",
        "owner_name": "api",
        "type": RecommendationType.RIGHTSIZING_CPU,
        "description": "resize",
    }
    first = RecommendationRecord(container_name="web", **common)
    second = RecommendationRecord(container_name="web", **common)
    other_container = RecommendationRecord(container_name="worker", **common)
    other_owner = RecommendationRecord(
        pod_name="pod-a",
        namespace="default",
        owner_kind="StatefulSet",
        owner_name="api",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="resize",
    )

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint == "v3:pod:default:pod-a:web::Deployment:api:RIGHTSIZING_CPU"
    assert first.fingerprint != other_container.fingerprint
    assert first.fingerprint != other_owner.fingerprint


def test_recommendation_payloads_are_typed_v2_models():
    recommendation = Recommendation(
        pod_name="pod-a",
        namespace="default",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="resize",
        patch={"operations": [{"path": "/spec/containers/0/resources"}]},
    )
    event = RecommendationEvent(
        recommendation_id=1,
        event_type="applied",
        payload={
            "baseline": {"metrics": {"cpu": 0.4}, "sample_count": 10},
            "patch": recommendation.patch,
        },
    )

    assert isinstance(recommendation.patch, RecommendationPatchV2)
    assert isinstance(event.payload, RecommendationEventPayload)
    assert isinstance(event.payload.baseline, RecommendationBaseline)
    assert event.payload.version == 2
