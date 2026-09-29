from greenkube.models.metrics import (
    Recommendation,
    RecommendationBaseline,
    RecommendationEvent,
    RecommendationEventPayload,
    RecommendationPatchV2,
    RecommendationRecord,
    RecommendationType,
)


def test_fingerprint_v2_is_stable_and_container_aware():
    common = {
        "pod_name": "pod-a",
        "namespace": "default",
        "type": RecommendationType.RIGHTSIZING_CPU,
        "description": "resize",
    }
    first = RecommendationRecord(container_name="web", **common)
    second = RecommendationRecord(container_name="web", **common)
    other_container = RecommendationRecord(container_name="worker", **common)

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint == "v2:pod:default:pod-a:web::RIGHTSIZING_CPU"
    assert first.fingerprint != other_container.fingerprint


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
