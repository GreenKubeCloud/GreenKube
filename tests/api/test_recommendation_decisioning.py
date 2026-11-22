# tests/api/test_recommendation_decisioning.py
"""Live decision on recommendation reads: expected value, eligibility and track record (F10, I1-I3)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.api.dependencies import get_gitops_binding_repository, get_pull_request_repository
from greenkube.core.config import get_config
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerResources,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import (
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
    RiskLevel,
)
from greenkube.storage.base_gitops_repository import WorkloadBinding

NOW = datetime.now(timezone.utc)


def _record(rec_id, name, cost, namespace="prod", co2=0.0, risk=RiskLevel.LOW, confidence=0.8, action=False):
    return RecommendationRecord(
        id=rec_id,
        pod_name=name,
        container_name=name,
        namespace=namespace,
        owner_kind="Deployment",
        owner_name=name,
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        description=f"reduce {name}",
        potential_savings_cost=cost,
        potential_savings_co2e_grams=co2,
        risk_level=risk,
        confidence=confidence,
        source="vpa",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        evidence=RecommendationEvidence(
            sample_count=500,
            coverage_ratio=0.95,
            observation_window_end=NOW,
            observation_window_seconds=7 * 86400,
        ),
        action=(
            ActionPlanV3(
                action_id=f"action-{rec_id}",
                recommendation_ids=[rec_id],
                target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace=namespace, name=name),
                operations=[
                    SetContainerResources(container=name, resources=ContainerResources(requests={"cpu": "300m"}))
                ],
                constraints={"container_targeted": True},
                evidence_ref=f"recommendation-evidence/{rec_id}",
            )
            if action
            else None
        ),
    )


RECORDS = [
    _record(1, "checkout", 300.0, namespace="prod", risk=RiskLevel.MEDIUM, action=True),
    _record(2, "search", 40.0, namespace="search", co2=2_000_000.0),
    _record(3, "batch", 900.0, namespace="batch", risk=RiskLevel.HIGH, confidence=0.4),
    _record(4, "web", 120.0, namespace="prod"),
]


@pytest.fixture
def decision_client(client, mock_reco_repo):
    mock_reco_repo.get_active_recommendations = AsyncMock(
        side_effect=lambda namespace=None, include_pr_open=False: [
            r for r in RECORDS if namespace in (None, r.namespace)
        ]
    )
    mock_reco_repo.get_recommendation_by_id = AsyncMock(side_effect=lambda rec_id: RECORDS[rec_id - 1])
    mock_reco_repo.get_outcome_counts_by_source_capability = AsyncMock(return_value=[])
    return client


def _ids(response):
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


class TestActiveDecision:
    def test_active_records_carry_value_and_eligibility(self, decision_client):
        rows = decision_client.get("/api/v1/recommendations/active").json()
        assert len(rows) == 4
        for row in rows:
            assert row["ranking_score"] == row["value"]["ev"]
            assert row["value"]["currency"]
            assert 0 < row["value"]["p_success"] < 1
            assert row["eligibility"]["can_open_pr"] is False
            codes = {b["code"] for b in row["eligibility"]["blockers"]}
            assert "automation_not_configured" in codes
        evs = [row["value"]["ev"] for row in rows]
        assert evs == sorted(evs, reverse=True)

    def test_order_is_stable_across_namespace_and_risk_filters(self, decision_client):
        full = _ids(decision_client.get("/api/v1/recommendations/active"))
        for query in ("namespace=prod", "risk_level=low", "namespace=batch", "source=vpa"):
            subset = _ids(decision_client.get(f"/api/v1/recommendations/active?{query}"))
            assert subset == [rec_id for rec_id in full if rec_id in subset], query

    def test_profile_and_carbon_price_per_request(self, decision_client):
        default = {r["id"]: r for r in decision_client.get("/api/v1/recommendations/active").json()}
        carbon = {r["id"]: r for r in decision_client.get("/api/v1/recommendations/active?carbon_price=1000").json()}
        cost_first = {
            r["id"]: r for r in decision_client.get("/api/v1/recommendations/active?profile=cost_first").json()
        }
        assert carbon[2]["value"]["components"]["carbon_price_per_tonne"] == 1000
        assert carbon[2]["value"]["value_annual"] == pytest.approx(40.0 + 2000.0)
        assert cost_first[2]["value"]["value_annual"] == pytest.approx(40.0)
        assert default[2]["value"]["profile"] == "balanced"
        assert cost_first[2]["value"]["profile"] == "cost_first"

    def test_unknown_profile_is_rejected(self, decision_client):
        assert decision_client.get("/api/v1/recommendations/active?profile=nope").status_code == 422
        assert decision_client.get("/api/v1/recommendations/active?carbon_price=-5").status_code == 422

    def test_detail_carries_the_decision(self, decision_client):
        row = decision_client.get("/api/v1/recommendations/1").json()
        assert row["eligibility"]["action_kind"] == "container_resources"
        assert row["value"]["ev"] == row["ranking_score"]

    def test_top_by_value(self, decision_client):
        rows = decision_client.get("/api/v1/recommendations/top?metric=value&limit=2").json()
        assert [row["rank"] for row in rows] == [1, 2]
        assert rows[0]["sort_metric"] == "value"
        assert rows[0]["sort_value"] == rows[0]["value"]["ev"]
        profile_rows = decision_client.get("/api/v1/recommendations/top?profile=low_risk&limit=4").json()
        assert profile_rows[0]["value"]["profile"] == "low_risk"
        assert all(row["sort_metric"] == "value" for row in profile_rows)

    def test_top_by_profile_matches_active_under_configured_costs(self, decision_client, mock_reco_repo, monkeypatch):
        """/top?profile= keeps the configured prices and the track record (no second, default scoring)."""
        config = get_config().model_copy(
            update={
                "CARBON_PRICE_PER_TONNE": 500.0,
                "RECOMMENDATION_INCIDENT_COST": 1000.0,
                "RECOMMENDATION_ENGINEER_HOUR_COST": 80.0,
            }
        )
        monkeypatch.setattr("greenkube.api.routers.recommendations.get_config", lambda: config)
        capability = "cpu_rightsizing"
        mock_reco_repo.get_outcome_counts_by_source_capability = AsyncMock(
            return_value=[{"source": "vpa", "capability": capability, "realized": 2, "not_realized": 6}]
        )

        active = decision_client.get("/api/v1/recommendations/active?profile=low_risk").json()
        top = decision_client.get("/api/v1/recommendations/top?profile=low_risk&limit=50").json()
        assert active[0]["value"]["components"]["carbon_price_per_tonne"] == 500.0
        assert [row["id"] for row in top] == [row["id"] for row in active]
        by_id = {row["id"]: row for row in active}
        for row in top:
            assert row["value"]["ev"] == by_id[row["id"]]["value"]["ev"]
            assert row["value"]["components"] == by_id[row["id"]]["value"]["components"]
            assert row["sort_value"] == row["value"]["ev"]


def _source():
    return WorkloadSource(
        id="src-1",
        cluster="default",
        namespace="prod",
        workload_kind="Deployment",
        workload_name="checkout",
        provider_id="github",
        repo_url="https://github.com/example/gitops.git",
        ref="main",
        path="apps/checkout",
        controller_kind="argocd",
        controller_name="checkout",
        controller_namespace="argocd",
        renderer="raw",
        confidence=1.0,
        status=BindingStatus.RESOLVED,
        evidence={
            "rollout_in_progress": False,
            "controller_suspended": False,
            "controller_healthy": True,
            "workload_labels": {"greenkube.io/criticality": "critical"},
        },
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


class TestEligibilityWithAutomation:
    @pytest.fixture
    def automation_client(self, decision_client, monkeypatch):
        config = get_config().model_copy(update={"AUTOMATION_ENABLED": True, "AUTOMATION_MODE": "inline"})
        monkeypatch.setattr("greenkube.api.routers.recommendations.get_config", lambda: config)
        # No live discovery evidence in this fixture; the default fails closed (D13).
        monkeypatch.setenv("GITOPS_POLICY_JSON", '{"missingEvidence": "warn"}')
        gitops = MagicMock()
        gitops.list_workload_bindings = AsyncMock(
            return_value=[
                WorkloadBinding(
                    cluster="default",
                    namespace="prod",
                    workload_kind="Deployment",
                    workload_name="checkout",
                    status=BindingStatus.RESOLVED,
                    confidence=1.0,
                    source=_source(),
                    observed_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                )
            ]
        )
        gitops.list_plan_records_for_workload = AsyncMock(return_value=[])
        prs = MagicMock()
        prs.get_open_pull_requests = AsyncMock(return_value=[])
        app = decision_client.app
        app.dependency_overrides[get_gitops_binding_repository] = lambda: gitops
        app.dependency_overrides[get_pull_request_repository] = lambda: prs
        app.state.automation_plan_service = object()
        yield decision_client
        app.state.automation_plan_service = None

    def test_resolved_binding_with_a_safe_action_can_open_a_pr(self, automation_client):
        rows = {row["id"]: row for row in automation_client.get("/api/v1/recommendations/active").json()}
        checkout = rows[1]["eligibility"]
        assert checkout["can_open_pr"] is True, checkout["blockers"]
        assert checkout["delivery_modes"] == ["direct", "manual"]
        assert checkout["recommendation_ids"] == [1]
        # Criticality read from the binding labels widens the blast radius.
        assert rows[1]["value"]["components"]["criticality"] == "critical"
        assert rows[1]["value"]["components"]["delivery_mode"] == "direct"

        web = {b["code"] for b in rows[4]["eligibility"]["blockers"]}
        assert web == {"action_unavailable"}
        batch = {b["code"] for b in rows[3]["eligibility"]["blockers"]}
        assert {"policy_risk_exceeded", "policy_confidence_too_low"} <= batch

    def test_unknown_oom_history_warns_but_a_person_can_open_the_pr(self, automation_client, mock_reco_repo):
        """2026-11-15: oom_history_unknown moved from blocker to warning; the cause is named."""
        flagged = RECORDS[0].model_copy(
            update={"risk_factors": ["oom_history_unknown", "oom_unknown_no_usage_summary"]}
        )
        mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[flagged])
        [row] = automation_client.get("/api/v1/recommendations/active").json()
        eligibility = row["eligibility"]
        assert eligibility["can_open_pr"] is True, eligibility["blockers"]
        assert eligibility["recommendation_ids"] == [1]
        [warning] = [w for w in eligibility["warnings"] if w["code"] == "oom_history_unknown"]
        assert "No usage summary covers this container" in warning["message"]
        assert "Before merging" in warning["fix"]

    def test_unreadable_pull_requests_fail_closed(self, automation_client):
        prs = automation_client.app.dependency_overrides[get_pull_request_repository]()
        prs.get_open_pull_requests = AsyncMock(side_effect=RuntimeError("database down"))
        rows = {row["id"]: row for row in automation_client.get("/api/v1/recommendations/active").json()}
        checkout = rows[1]["eligibility"]
        assert checkout["can_open_pr"] is False
        assert [b["code"] for b in checkout["blockers"]] == ["pull_requests_unavailable"]
        assert checkout["delivery_modes"] == ["manual"]


class TestTrackRecordEndpoint:
    def test_derived_from_recommendation_status(self, client, mock_reco_repo):
        del mock_reco_repo.get_outcome_counts_by_source_capability
        mock_reco_repo.get_recommendations_by_statuses = AsyncMock(
            return_value=[
                RecommendationRecord(
                    pod_name="api",
                    namespace="prod",
                    type=RecommendationType.RIGHTSIZING_CPU,
                    description="x",
                    status=status,
                    verification_status=verification,
                    source="vpa",
                )
                for status, verification in (
                    (RecommendationStatus.VERIFIED, "passed"),
                    (RecommendationStatus.VERIFIED, "passed"),
                    (RecommendationStatus.APPLIED, "failed"),
                )
            ]
        )
        body = client.get("/api/v1/sources/track-record").json()
        assert body["method"] == "recommendation_verification_status"
        assert body["prior"] == {"alpha": 1.0, "beta": 1.0}
        (entry,) = body["entries"]
        assert entry["source_family"] == "vpa" and entry["capability"] == "cpu_rightsizing"
        assert (entry["alpha"], entry["beta"]) == (3.0, 2.0)
        assert entry["summary"] == "vpa: 2/3 realized"

    def test_track_record_lowers_p_success(self, decision_client, mock_reco_repo):
        before = {
            r["id"]: r["value"]["p_success"] for r in decision_client.get("/api/v1/recommendations/active").json()
        }
        mock_reco_repo.get_outcome_counts_by_source_capability = AsyncMock(
            return_value=[{"source": "vpa", "capability": "cpu_rightsizing", "not_realized": 30}]
        )
        after = {r["id"]: r["value"]["p_success"] for r in decision_client.get("/api/v1/recommendations/active").json()}
        assert all(after[rec_id] < before[rec_id] for rec_id in before)


@pytest.fixture(autouse=True)
def _wall_clock_at_test_time(monkeypatch):
    """Re-read ``NOW`` for each test: a wall-clock constant taken at import ages during a long suite."""
    monkeypatch.setitem(globals(), "NOW", datetime.now(timezone.utc))
