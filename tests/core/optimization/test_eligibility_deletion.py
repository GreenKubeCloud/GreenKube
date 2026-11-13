"""Eligibility of deletion pull requests (roadmap H6): offered, reviewed, never auto-approved."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.automation.actions import ACTION_KINDS
from greenkube.core.optimization.eligibility import (
    AutoApprovePolicy,
    EligibilityContext,
    assess_eligibility,
    auto_approve_blockers,
    load_resource_bindings,
)
from greenkube.gitops.contracts import ControllerKind
from greenkube.gitops.discovery.inventory import ResourceBinding
from greenkube.models.metrics import (
    Recommendation,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
    RiskLevel,
)
from greenkube.storage.resource_binding_repository import StoredResourceBinding
from tests.gitops.planning.test_raw_manifest import _source  # pyrefly: ignore[missing-import]

NOW = datetime.now(timezone.utc)
KEY = ("Service", "shop", "old-lb")


def _record(**overrides) -> RecommendationRecord:
    recommendation = Recommendation(
        id=21,
        pod_name="old-lb",
        namespace="shop",
        type=RecommendationType.ORPHANED_LOAD_BALANCER,
        scope="cluster",
        description="x",
    )
    values = dict(
        id=21,
        pod_name="old-lb",
        namespace="shop",
        type=RecommendationType.ORPHANED_LOAD_BALANCER,
        scope="cluster",
        description="LoadBalancer Service 'old-lb' appears unused",
        risk_level=RiskLevel.MEDIUM,
        confidence=0.9,
        status=RecommendationStatus.ACTIVE,
        action=ACTION_KINDS.build_action_plans([recommendation])[21],
    )
    values.update(overrides)
    return RecommendationRecord(**values)


def _stored(*, prune: bool | None = True, status: str = "resolved", location=True, age=timedelta(0)):
    binding = ResourceBinding(
        ref=_record().action.target,  # type: ignore[union-attr]
        status=status,  # type: ignore[arg-type]
        controllers=((ControllerKind.ARGOCD, "argocd", "shop"),),
        location=_source() if location else None,
        reason=None if location else "controller-source-unresolved",
        prune=prune,
    )
    return StoredResourceBinding(binding=binding, observed_at=NOW - age)


def _ctx(**overrides) -> EligibilityContext:
    from greenkube.automation.policy import AutomationPolicyConfig

    values = dict(
        now=NOW,
        automation_configured=True,
        automation_enabled=True,
        bindings={},
        resource_bindings={KEY: _stored()},
        policy=AutomationPolicyConfig(max_risk_level="high"),
    )
    values.update(overrides)
    return EligibilityContext(**values)


def _codes(entries) -> list[str]:
    return [entry["code"] for entry in entries]


def test_a_bound_orphaned_load_balancer_can_open_a_reviewed_pull_request() -> None:
    result = assess_eligibility(_record(), _ctx())

    assert result["can_open_pr"] is True, result["blockers"]
    assert result["action_kind"] == "delete_resource"
    assert result["recommendation_ids"] == [21]
    assert "deletion_reviewed_in_pr" in _codes(result["warnings"])
    assert "deletion_prune_disabled" not in _codes(result["warnings"])


def test_a_controller_without_prune_is_a_warning() -> None:
    result = assess_eligibility(_record(), _ctx(resource_bindings={KEY: _stored(prune=None)}))
    assert result["can_open_pr"] is True
    assert "deletion_prune_disabled" in _codes(result["warnings"])


@pytest.mark.parametrize(
    ("resource_bindings", "code"),
    [
        (None, "resource_binding_unavailable"),
        ({}, "resource_binding_missing"),
        ({KEY: _stored(status="ambiguous")}, "ambiguous_binding"),
        ({KEY: _stored(location=False)}, "binding_unresolved"),
        ({KEY: _stored(age=timedelta(days=2))}, "binding_stale"),
    ],
)
def test_unbound_resources_stay_manual(resource_bindings, code) -> None:
    result = assess_eligibility(_record(), _ctx(resource_bindings=resource_bindings))
    assert result["can_open_pr"] is False
    assert code in _codes(result["blockers"])
    assert result["delivery_modes"] == ["manual"]


def test_the_default_risk_policy_still_blocks_a_high_risk_deletion() -> None:
    from greenkube.automation.policy import AutomationPolicyConfig

    result = assess_eligibility(_record(risk_level=RiskLevel.HIGH), _ctx(policy=AutomationPolicyConfig()))
    assert "policy_risk_exceeded" in _codes(result["blockers"])


def test_a_foreign_or_missing_action_is_refused() -> None:
    assert "action_unavailable" in _codes(assess_eligibility(_record(action=None), _ctx())["blockers"])
    other = _record(pod_name="another-lb")
    assert "action_unavailable" in _codes(assess_eligibility(other, _ctx())["blockers"])


def test_deletions_are_never_auto_approved() -> None:
    policy = AutoApprovePolicy(enabled=True, max_risk="high", min_confidence=0.0)
    assert "deletion_requires_review" in auto_approve_blockers([_record()], policy, expected=1)


class _Gitops:
    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager


@pytest.mark.asyncio
async def test_unreadable_resource_bindings_fail_closed() -> None:
    assert await load_resource_bindings(object(), "prod") is None

    class _Broken:
        db_type = "sqlite"

        def connection_scope(self):
            raise RuntimeError("database is down")

    assert await load_resource_bindings(_Gitops(_Broken()), "prod") is None
