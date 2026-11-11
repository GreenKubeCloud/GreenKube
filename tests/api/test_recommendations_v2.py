"""GET /api/v2/recommendations and the v1 compatibility of the same records (roadmap E4, E6, E7)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.recommendation_v2 import FLAT_FIELDS, RecommendationRevision, RecommendationV2, flat_fields

CONSENSUS = {
    "policy": "safest",
    "resource": "cpu_request_m",
    "value": 300,
    "chosen_from": ["vpa"],
    "agreement": 0.8333,
    "contested": False,
    "reasons": [],
    "spread": {"cpu_request_m": {"vpa": 300, "krr": 250, "greenkube.rightsizing_cpu": 280}},
    "independent_votes": 3,
    "votes": [
        {"source_id": "vpa", "source": "vpa", "family": "vpa", "signal_id": "s1", "value": 300, "counted": True},
        {"source_id": "krr", "source": "krr", "family": "krr", "signal_id": "s2", "value": 250, "counted": True},
    ],
}


def _record(**overrides) -> RecommendationRecord:
    values = dict(
        id=12,
        pod_name="api",
        namespace="shop",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="VPA 300m · KRR 250m · GreenKube 280m → 300m",
        reason="Consensus across sources.",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=300,
        potential_savings_cost=42.0,
        source="vpa",
        sources=["vpa", "krr", "greenkube"],
        fingerprint="legacy-fp",
        revision=3,
        consensus=CONSENSUS,
        created_at=datetime.now(timezone.utc),
    )
    values.update(overrides)
    return RecommendationRecord(**values)


class TestListV2:
    def test_lists_active_recommendations_in_the_v2_shape(self, client, mock_reco_repo):
        mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[_record()])
        mock_reco_repo.get_queued_revisions = AsyncMock(return_value={})

        response = client.get("/api/v2/recommendations")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        [item] = body["items"]
        assert item["id"] == 12
        assert item["revision"] == 3
        assert item["queued_revision"] is None
        assert item["capability"] == "container_requests"
        assert item["kind"] == "RIGHTSIZING_CPU"
        assert item["target"]["kind"] == "Deployment" and item["target"]["container"] == "web"
        assert item["proposal"]["kind"] == "container_resources"
        assert item["proposal"]["proposed"]["cpu_request_m"] == 300
        assert item["consensus"]["agreement"] == 0.8333
        assert [signal["source_id"] for signal in item["signals"]] == ["vpa", "krr"]
        assert item["source"] == "vpa" and item["sources"] == ["vpa", "krr", "greenkube"]
        assert item["eligibility"] is not None
        assert len(item["fingerprint"]) == 64 and item["legacy_fingerprint"] == "legacy-fp"
        mock_reco_repo.get_active_recommendations.assert_awaited_with(namespace=None, include_pr_open=True)

    def test_queued_advice_is_reported(self, client, mock_reco_repo):
        mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[_record()])
        mock_reco_repo.get_queued_revisions = AsyncMock(
            return_value={12: RecommendationRevision(recommendation_id=12, revision=4, state="queued")}
        )

        [item] = client.get("/api/v2/recommendations").json()["items"]

        assert item["queued_revision"] == 4

    def test_filters_by_status_capability_and_source(self, client, mock_reco_repo):
        memory = _record(
            id=13,
            type=RecommendationType.RIGHTSIZING_MEMORY,
            recommended_cpu_request_millicores=None,
            current_cpu_request_millicores=None,
            recommended_memory_request_bytes=256,
            current_memory_request_bytes=512,
            source="greenkube",
            sources=["greenkube"],
            consensus=None,
        )
        pv = _record(
            id=14,
            type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
            pod_name="pv-1",
            scope="cluster",
            owner_kind=None,
            owner_name=None,
            container_name=None,
            recommended_cpu_request_millicores=None,
            current_cpu_request_millicores=None,
            consensus=None,
            status=RecommendationStatus.APPLIED,
        )
        mock_reco_repo.get_recommendations_by_statuses = AsyncMock(return_value=[_record(), memory, pv])
        mock_reco_repo.get_queued_revisions = AsyncMock(return_value={})

        response = client.get(
            "/api/v2/recommendations",
            params={"status": "active,applied", "capability": "container_requests", "source": "krr"},
        )

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [12]
        mock_reco_repo.get_recommendations_by_statuses.assert_awaited_with(["active", "applied"], namespace=None)

    def test_unknown_status_is_rejected(self, client):
        assert client.get("/api/v2/recommendations", params={"status": "maybe"}).status_code == 422

    def test_provenance_source_ids_are_listed_and_filterable(self, client, mock_reco_repo):
        """Goldilocks votes under the VPA reconciliation unit; its id still reaches the UI badges (G1)."""
        consensus = {**CONSENSUS, "source_ids": ["goldilocks", "vpa", "krr"]}
        legacy = _record(id=15, consensus=None, sources=["greenkube"], source="greenkube")
        mock_reco_repo.get_active_recommendations = AsyncMock(
            return_value=[_record(consensus=consensus, sources=["vpa", "krr"]), legacy]
        )
        mock_reco_repo.get_queued_revisions = AsyncMock(return_value={})

        items = client.get("/api/v2/recommendations").json()["items"]
        by_id = {item["id"]: item for item in items}
        assert by_id[12]["source_ids"] == ["goldilocks", "vpa", "krr"]
        assert by_id[12]["sources"] == ["vpa", "krr"]
        # A record without a consensus falls back to its reconciliation units.
        assert by_id[15]["source_ids"] == ["greenkube"]

        filtered = client.get("/api/v2/recommendations", params={"source": "goldilocks"}).json()["items"]
        assert [item["id"] for item in filtered] == [12]


class TestDetailV2:
    def test_detail_carries_the_revision_history(self, client, mock_reco_repo):
        mock_reco_repo.get_recommendation_by_id = AsyncMock(return_value=_record())
        mock_reco_repo.get_revisions = AsyncMock(
            return_value=[
                RecommendationRevision(
                    recommendation_id=12,
                    revision=4,
                    state="queued",
                    snapshot={"recommended_cpu_request_millicores": 260},
                ),
                RecommendationRevision(recommendation_id=12, revision=2, state="superseded"),
            ]
        )

        response = client.get("/api/v2/recommendations/12")

        assert response.status_code == 200
        body = response.json()
        assert body["queued_revision"] == 4
        assert [(item["revision"], item["state"]) for item in body["revisions"]] == [(4, "queued"), (2, "superseded")]

    def test_unknown_recommendation_is_404(self, client, mock_reco_repo):
        mock_reco_repo.get_recommendation_by_id = AsyncMock(return_value=None)
        assert client.get("/api/v2/recommendations/99").status_code == 404


class TestV1Compatibility:
    def test_v1_flat_fields_match_the_v2_proposal(self, client, mock_reco_repo):
        """v1 -> v2 -> v1: the flat fields of /api/v1 are the v2 proposal in flat form."""
        record = _record()
        mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[record])
        mock_reco_repo.get_recommendation_by_id = AsyncMock(return_value=record)
        mock_reco_repo.get_queued_revisions = AsyncMock(return_value={})
        mock_reco_repo.get_revisions = AsyncMock(return_value=[])

        [v1] = client.get("/api/v1/recommendations").json()
        v2 = RecommendationV2.model_validate(client.get("/api/v2/recommendations/12").json())

        flat = flat_fields(v2)
        assert {name: flat.get(name) for name in FLAT_FIELDS} == {name: v1[name] for name in FLAT_FIELDS}
        # v1 keeps its shape and gains the v2 fields additively.
        assert v1["recommended_cpu_request_millicores"] == 300
        assert v1["revision"] == 3 and v1["consensus"]["policy"] == "safest"


class TestWarmUpEta:
    def test_v1_and_v2_expose_when_a_warming_recommendation_becomes_eligible(self, client, mock_reco_repo):
        """F7: 'Collecting evidence — eligible in ~Nh' is computed server-side on both API versions."""
        from datetime import timedelta

        from greenkube.models.evidence import RecommendationEvidence

        now = datetime.now(timezone.utc)
        evidence = RecommendationEvidence(
            observation_window_start=now - timedelta(minutes=30),
            observation_window_end=now,
            observation_window_seconds=7 * 86400,
            sample_count=6,
            coverage_ratio=0.9,
        )
        record = _record(evidence=evidence, consensus=None, source="greenkube", sources=["greenkube"])
        mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[record])
        mock_reco_repo.get_queued_revisions = AsyncMock(return_value={})

        [v2] = client.get("/api/v2/recommendations").json()["items"]
        v1 = client.get("/api/v1/recommendations").json()

        eligible_at = datetime.fromisoformat(v2["eligibility"]["eligible_at"])
        assert now < eligible_at <= now + timedelta(days=1)
        assert "warming_up" in [blocker["code"] for blocker in v2["eligibility"]["blockers"]]
        [v1_item] = v1 if isinstance(v1, list) else v1["items"]
        assert v1_item["eligibility"]["eligible_at"] == v2["eligibility"]["eligible_at"]
