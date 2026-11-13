"""The ``delete_resource`` action kind (roadmap H6)."""

from __future__ import annotations

import pytest

from greenkube.automation.actions import (
    ACTION_KINDS,
    DELETE_RESOURCE,
    ActionKindError,
    InverseUnsupportedError,
)
from greenkube.automation.actions.delete_resource import deletion_target_for
from greenkube.gitops.contracts import DeleteResource, RendererKind, ResourceTarget
from greenkube.models.metrics import Recommendation, RecommendationType


def _lb(**overrides) -> Recommendation:
    values = {
        "id": 11,
        "pod_name": "old-lb",
        "namespace": "shop",
        "type": RecommendationType.ORPHANED_LOAD_BALANCER,
        "scope": "cluster",
        "description": "LoadBalancer Service 'old-lb' appears unused",
    }
    values.update(overrides)
    return Recommendation(**values)


def _pv(**overrides) -> Recommendation:
    values = {
        "id": 12,
        "pod_name": "pv-orphan",
        "type": RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        "scope": "cluster",
        "description": "PersistentVolume 'pv-orphan' appears unused",
    }
    values.update(overrides)
    return Recommendation(**values)


def test_deletion_recommendations_become_delete_actions() -> None:
    actions = ACTION_KINDS.build_action_plans([_lb(), _pv()])

    lb, pv = actions[11], actions[12]
    assert lb.target == ResourceTarget(api_version="v1", kind="Service", namespace="shop", name="old-lb")
    assert pv.target == ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-orphan")
    assert ACTION_KINDS.spec_for_action(lb) is DELETE_RESOURCE
    assert lb.constraints["resource_deletion"] is True
    assert DELETE_RESOURCE.matches_recommendation(_lb(), lb.target)
    assert not DELETE_RESOURCE.matches_recommendation(_pv(), lb.target)


def test_zombies_idle_namespaces_and_incomplete_records_get_no_action() -> None:
    zombie = _lb(id=13, type=RecommendationType.ZOMBIE_POD)
    idle = _lb(id=14, type=RecommendationType.IDLE_NAMESPACE)
    no_namespace = _lb(id=15, namespace=None)
    assert ACTION_KINDS.build_action_plans([zombie, idle, no_namespace]) == {}
    assert deletion_target_for(no_namespace) is None


def test_the_kind_is_high_risk_never_auto_approved_and_resource_bound() -> None:
    assert DELETE_RESOURCE.risk_class == "high"
    assert DELETE_RESOURCE.auto_approvable is False
    assert DELETE_RESOURCE.binding_scope == "resource"
    assert not DELETE_RESOURCE.requires_workload_baseline
    assert ("", "PersistentVolume") in DELETE_RESOURCE.resource_kinds
    assert DELETE_RESOURCE.measurement_strategy == "delete_resource"


def test_helm_renderers_are_refused_with_an_explanation() -> None:
    support = DELETE_RESOURCE.renderer_support(RendererKind.HELM_VALUES, [DeleteResource()])
    assert not support.ok and "values toggle" in (support.reason or "")
    assert DELETE_RESOURCE.renderer_support(RendererKind.KUSTOMIZE, [DeleteResource()]).ok


def test_baseline_capture_requires_the_live_resource_and_respects_opt_outs() -> None:
    target = ResourceTarget(api_version="v1", kind="Service", namespace="shop", name="old-lb")
    assert DELETE_RESOURCE.capture_baseline({"metadata": {"name": "old-lb"}}, target) is None
    with pytest.raises(ActionKindError, match="no_effective_change"):
        DELETE_RESOURCE.capture_baseline(None, target)
    with pytest.raises(ActionKindError, match="opted out"):
        DELETE_RESOURCE.capture_baseline({"metadata": {"annotations": {"greenkube.io/keep": ""}}}, target)


def test_apply_detection_is_absence() -> None:
    target = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-orphan")
    assert DELETE_RESOURCE.is_resource_applied(None, target).applied
    verdict = DELETE_RESOURCE.is_resource_applied({"metadata": {}}, target)
    assert not verdict.applied and "still exists" in (verdict.reason or "")


def test_a_deletion_has_no_automated_rollback() -> None:
    assert not DELETE_RESOURCE.supports_rollback([DeleteResource()])
    with pytest.raises(InverseUnsupportedError, match="no automated rollback"):
        DELETE_RESOURCE.inverse([DeleteResource()], None)  # type: ignore[arg-type]


def test_pr_section_names_the_resource() -> None:
    section = DELETE_RESOURCE.pr_section([DeleteResource()], _pv())
    assert section.label == "cleanup"
    assert section.lines == ("Delete PersistentVolume pv-orphan from the repository",)
