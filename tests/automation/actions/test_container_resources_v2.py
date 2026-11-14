"""Container resources v2 (H1): increases, limits, native sidecars and more workload kinds."""

from __future__ import annotations

import hashlib
from typing import Literal

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from greenkube.automation.actions import CONTAINER_RESOURCES, ActionKindError
from greenkube.automation.actions.container_resources import build_container_resource_actions
from greenkube.automation.actions.primitives import RemoveField, SetField, parse_path
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerRequestBaseline,
    ContainerResources,
    ControllerKind,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.planning import RawManifestPlanError, RawManifestPlanner, RepositorySnapshot
from greenkube.models.evidence import (
    LimitChange,
    PatchV2,
    ProposedChange,
    RecommendationEvidence,
    ResourceSnapshot,
)
from greenkube.models.metrics import Recommendation, RecommendationType

MIB = 1024**2
BASE_REVISION = "a" * 40
DEPLOYMENT_TARGET = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api")


def _recommendation(
    *,
    recommendation_id: int = 7,
    rec_type: RecommendationType = RecommendationType.RIGHTSIZING_MEMORY,
    current: int = 512 * MIB,
    proposed: int = 768 * MIB,
    owner_kind: str = "Deployment",
    container: str = "api",
    current_limit: int | None = None,
    limit_changes: list[LimitChange] | None = None,
    container_kind: Literal["container", "sidecar", "init"] | None = None,
    sample_count: int = 2016,
    source: str = "greenkube",
    consensus: dict | None = None,
) -> Recommendation:
    memory = rec_type == RecommendationType.RIGHTSIZING_MEMORY
    resource = "memory" if memory else "cpu"
    current_snapshot = (
        ResourceSnapshot(memory_request_bytes=current, memory_limit_bytes=current_limit)
        if memory
        else ResourceSnapshot(cpu_request_millicores=current, cpu_limit_millicores=current_limit)
    )
    proposed_snapshot = (
        ResourceSnapshot(memory_request_bytes=proposed) if memory else ResourceSnapshot(cpu_request_millicores=proposed)
    )
    evidence = RecommendationEvidence(
        current=current_snapshot,
        proposed=proposed_snapshot,
        changes=[ProposedChange(resource=resource, current=current, proposed=proposed)],
        proposed_patch=PatchV2(container_name=container),
        limit_changes=limit_changes or [],
        container_kind=container_kind,
        sample_count=sample_count,
    )
    return Recommendation(
        id=recommendation_id,
        pod_name="api",
        container_name=container,
        namespace="payments",
        type=rec_type,
        description="Raise the request.",
        scope="workload",
        owner_kind=owner_kind,
        owner_name="api",
        evidence=evidence,
        current_memory_request_bytes=current if memory else None,
        recommended_memory_request_bytes=proposed if memory else None,
        current_cpu_request_millicores=None if memory else current,
        recommended_cpu_request_millicores=None if memory else proposed,
        source=source,
        consensus=consensus,
    )


def _only_operation(recommendation: Recommendation) -> SetContainerResources:
    actions = build_container_resource_actions([recommendation])
    action = actions[recommendation.id or 0]
    (operation,) = action.operations
    assert isinstance(operation, SetContainerResources)
    return operation


# -- recommendations -> actions ---------------------------------------------------------------


def test_a_request_increase_becomes_an_action() -> None:
    """Regression (H1): increases were advisory, ``proposed >= current`` returned no action."""
    operation = _only_operation(_recommendation())
    assert operation.resources.requests == {"memory": "768Mi"}
    assert operation.resources.limits == {}
    assert operation.remove_limits == []
    assert operation.container_kind == "container"


@pytest.mark.parametrize(
    ("source", "consensus", "sample_count"),
    [
        ("vpa", {"chosen_from": ["vpa"], "independent_votes": 1}, 2016),
        ("goldilocks", {"chosen_from": ["goldilocks", "vpa"], "independent_votes": 1}, 2016),
        ("push:ci", {"chosen_from": ["push:ci"], "independent_votes": 1}, 0),
        ("greenkube", None, 0),
    ],
)
def test_an_increase_not_sized_by_greenkube_stays_advisory(
    source: str, consensus: dict | None, sample_count: int
) -> None:
    """Regression (review, H1): the wave-3a increase bounds (pod-bounded usage, 2x cap, warm-up
    exclusion) live in the native analyzer only. A 5x VPA increase chosen by the SAFEST consensus
    must not become a deliverable action."""
    recommendation = _recommendation(
        current=512 * MIB, proposed=5 * 512 * MIB, source=source, consensus=consensus, sample_count=sample_count
    )
    assert build_container_resource_actions([recommendation]) == {}


def test_an_increase_greenkube_agrees_with_is_an_action() -> None:
    recommendation = _recommendation(
        source="vpa", consensus={"chosen_from": ["vpa", "greenkube.rightsizing_memory"], "independent_votes": 2}
    )
    assert _only_operation(recommendation).resources.requests == {"memory": "768Mi"}


def test_external_decreases_keep_their_actions() -> None:
    """Only increases need GreenKube's sizing: decreases from other sources are unchanged."""
    recommendation = _recommendation(
        current=768 * MIB, proposed=512 * MIB, source="vpa", consensus={"chosen_from": ["vpa"]}, sample_count=0
    )
    assert _only_operation(recommendation).resources.requests == {"memory": "512Mi"}


def test_a_decrease_action_serializes_exactly_as_before() -> None:
    """Decreases keep their pre-H1 operation JSON, so action ids, digests and golden plans do not move."""
    operation = _only_operation(_recommendation(current=768 * MIB, proposed=512 * MIB))
    assert operation.model_dump(mode="json") == {
        "op": "set_container_resources",
        "container": "api",
        "resources": {"requests": {"memory": "512Mi"}, "limits": {}},
    }


def test_an_increase_above_the_current_limit_without_a_limit_change_has_no_action() -> None:
    """Never propose a change that cannot apply: the new request would exceed the limit."""
    recommendation = _recommendation(current_limit=640 * MIB)
    assert build_container_resource_actions([recommendation]) == {}


def test_an_increase_carries_its_raised_limit() -> None:
    recommendation = _recommendation(
        current_limit=640 * MIB,
        limit_changes=[
            LimitChange(resource="memory", action="set", current=640 * MIB, proposed=1024 * MIB, reason="below_floor")
        ],
    )
    operation = _only_operation(recommendation)
    assert operation.resources.requests == {"memory": "768Mi"}
    assert operation.resources.limits == {"memory": "1Gi"}


def test_a_limit_below_the_new_request_is_refused() -> None:
    recommendation = _recommendation(
        limit_changes=[LimitChange(resource="memory", action="set", current=None, proposed=600 * MIB)]
    )
    assert build_container_resource_actions([recommendation]) == {}


def test_a_memory_limit_removal_is_refused() -> None:
    recommendation = _recommendation(limit_changes=[LimitChange(resource="memory", action="remove", current=MIB)])
    assert build_container_resource_actions([recommendation]) == {}


def test_a_cpu_limit_removal_is_carried() -> None:
    recommendation = _recommendation(
        rec_type=RecommendationType.RIGHTSIZING_CPU,
        current=200,
        proposed=400,
        current_limit=300,
        limit_changes=[LimitChange(resource="cpu", action="remove", current=300, reason="policy")],
    )
    operation = _only_operation(recommendation)
    assert operation.resources.requests == {"cpu": "400m"}
    assert operation.remove_limits == ["cpu"]


def test_cpu_and_memory_recommendations_merge_their_limits() -> None:
    cpu = _recommendation(
        recommendation_id=1,
        rec_type=RecommendationType.RIGHTSIZING_CPU,
        current=200,
        proposed=400,
        limit_changes=[LimitChange(resource="cpu", action="set", current=300, proposed=600)],
    )
    memory = _recommendation(
        recommendation_id=2,
        limit_changes=[LimitChange(resource="memory", action="set", current=640 * MIB, proposed=1024 * MIB)],
    )
    actions = build_container_resource_actions([cpu, memory])
    assert actions[1] is actions[2]
    (operation,) = actions[1].operations
    assert isinstance(operation, SetContainerResources)
    assert operation.resources.requests == {"cpu": "400m", "memory": "768Mi"}
    assert operation.resources.limits == {"cpu": "600m", "memory": "1Gi"}


def test_a_native_sidecar_is_addressed_in_init_containers() -> None:
    operation = _only_operation(_recommendation(container="proxy", container_kind="sidecar"))
    assert operation.container_kind == "sidecar"
    (primitive,) = CONTAINER_RESOURCES.compile(DEPLOYMENT_TARGET, [operation])
    assert isinstance(primitive, SetField)
    assert primitive.path == "/spec/template/spec/initContainers/[name=proxy]/resources/requests/memory"


@pytest.mark.parametrize(
    ("owner_kind", "api_version", "prefix"),
    [
        ("CronJob", "batch/v1", "/spec/jobTemplate/spec/template/spec/containers"),
        ("Job", "batch/v1", "/spec/template/spec/containers"),
        ("Rollout", "argoproj.io/v1alpha1", "/spec/template/spec/containers"),
    ],
)
def test_cronjob_job_and_argo_rollout_targets(owner_kind: str, api_version: str, prefix: str) -> None:
    recommendation = _recommendation(owner_kind=owner_kind)
    action = build_container_resource_actions([recommendation])[7]
    assert action.target.kind == owner_kind
    assert action.target.api_version == api_version
    CONTAINER_RESOURCES.validate_operations(action.target, action.operations)
    (primitive,) = CONTAINER_RESOURCES.compile(action.target, action.operations)
    assert isinstance(primitive, SetField)
    assert primitive.path.startswith(prefix)


# -- the kind ------------------------------------------------------------------------------------


def test_compile_removes_the_cpu_limit() -> None:
    operation = SetContainerResources(
        container="api", resources=ContainerResources(requests={"cpu": "400m"}), remove_limits=["cpu"]
    )
    primitives = CONTAINER_RESOURCES.compile(DEPLOYMENT_TARGET, [operation])
    assert [type(primitive) for primitive in primitives] == [SetField, RemoveField]
    assert isinstance(primitives[1], RemoveField)
    assert primitives[1].path == "/spec/template/spec/containers/[name=api]/resources/limits/cpu"
    assert CONTAINER_RESOURCES.changed_fields([operation]) == {("api", "cpu")}


def test_the_contract_refuses_setting_and_removing_one_limit() -> None:
    with pytest.raises(ValidationError, match="both set and removed"):
        SetContainerResources(container="api", resources=ContainerResources(limits={"cpu": "1"}), remove_limits=["cpu"])


def test_the_kind_never_removes_a_memory_limit() -> None:
    operation = SetContainerResources(
        container="api", resources=ContainerResources(requests={"memory": "1Gi"}), remove_limits=["memory"]
    )
    with pytest.raises(ActionKindError, match="never removes a memory limit"):
        CONTAINER_RESOURCES.validate_operations(DEPLOYMENT_TARGET, [operation])


def test_limit_changes_have_no_automated_rollback_but_sidecar_requests_do() -> None:
    limits = SetContainerResources(
        container="api", resources=ContainerResources(requests={"cpu": "400m"}, limits={"cpu": "600m"})
    )
    removal = SetContainerResources(
        container="api", resources=ContainerResources(requests={"cpu": "400m"}), remove_limits=["cpu"]
    )
    sidecar = SetContainerResources(
        container="proxy", resources=ContainerResources(requests={"cpu": "40m"}), container_kind="sidecar"
    )
    assert not CONTAINER_RESOURCES.supports_rollback([limits])
    assert not CONTAINER_RESOURCES.supports_rollback([removal])
    assert CONTAINER_RESOURCES.supports_rollback([sidecar])
    baseline = PreApplyWorkloadBaseline(
        workload_uid="uid",
        generation=2,
        observed_generation=2,
        container_requests={"proxy": ContainerRequestBaseline(cpu_millicores=20, memory_bytes=None)},
    )
    (inverse,) = CONTAINER_RESOURCES.inverse([sidecar], baseline)
    assert isinstance(inverse, SetContainerResources)
    assert inverse.container_kind == "sidecar"
    assert inverse.resources.requests == {"cpu": "20m"}


def test_pr_section_lists_a_removed_limit() -> None:
    operation = SetContainerResources(
        container="api", resources=ContainerResources(requests={"cpu": "400m"}), remove_limits=["cpu"]
    )
    section = CONTAINER_RESOURCES.pr_section([operation])
    assert section.lines == ("api: requests cpu=400m, limits cpu removed",)
    assert [(change.field, change.proposed) for change in section.changes] == [
        ("requests.cpu", "400m"),
        ("limits.cpu", "removed"),
    ]


def test_helm_renderers_refuse_removals_and_sidecars() -> None:
    removal = SetContainerResources(
        container="api", resources=ContainerResources(requests={"cpu": "400m"}), remove_limits=["cpu"]
    )
    sidecar = SetContainerResources(
        container="proxy", resources=ContainerResources(requests={"cpu": "40m"}), container_kind="sidecar"
    )
    for renderer in (RendererKind.HELM_VALUES, RendererKind.HELM_RELEASE):
        assert not CONTAINER_RESOURCES.renderer_support(renderer, [removal]).ok
        assert not CONTAINER_RESOURCES.renderer_support(renderer, [sidecar]).ok
    assert CONTAINER_RESOURCES.renderer_support(RendererKind.KUSTOMIZE, [removal]).ok
    assert CONTAINER_RESOURCES.renderer_support(RendererKind.RAW, [sidecar]).ok


def test_managed_patches_own_limits_and_a_removed_cpu_limit() -> None:
    def owns(path: str, value: object) -> bool:
        return CONTAINER_RESOURCES.owns_patch_field("Deployment", parse_path(path), value)

    base = "/spec/template/spec/containers/[name=api]/resources"
    assert owns(f"{base}/requests/cpu", "100m")
    assert owns(f"{base}/limits/memory", "1Gi")
    assert owns(f"{base}/limits/cpu", None)
    assert not owns(f"{base}/limits/memory", None)
    assert not owns(f"{base}/requests/cpu", None)
    assert owns("/spec/template/spec/initContainers/[name=proxy]/resources/requests/cpu", "10m")
    assert not owns(f"{base}/requests/ephemeral-storage", "1Gi")


# -- raw manifests -------------------------------------------------------------------------------

RAW_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      initContainers:
        - name: proxy
          image: example/proxy:v1
          restartPolicy: Always
          resources:
            requests:
              cpu: 20m
      containers:
        - name: api
          image: example/api:v1
          resources:
            requests:
              cpu: 200m
              memory: 512Mi
            limits:
              cpu: 300m
              memory: 640Mi
"""

RAW_ROLLOUT = """apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: api
  namespace: payments
spec:
  strategy:
    canary: {}
  template:
    spec:
      containers:
        - name: api
          image: example/api:v1
          resources:
            requests:
              memory: 512Mi
"""


def _source(kind: str = "Deployment") -> WorkloadSource:
    return WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind=kind,
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        revision=BASE_REVISION,
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments-prod",
        controller_namespace="argocd",
        source_revision=BASE_REVISION,
        renderer=RendererKind.RAW,
        renderer_config={},
        confidence=0.98,
        status=BindingStatus.RESOLVED,
        evidence={},
    )


def _raw_plan(operations: list[SetContainerResources], content: str = RAW_DEPLOYMENT, kind: str = "Deployment"):
    path = "apps/payments/workload.yaml"
    encoded = content.encode()
    snapshot = RepositorySnapshot(
        contents={path: content},
        original_blob_shas={path: hashlib.sha1(f"blob {len(encoded)}\0".encode() + encoded).hexdigest()},
    )
    target = ResourceTarget(
        api_version="argoproj.io/v1alpha1" if kind == "Rollout" else "apps/v1",
        kind=kind,
        namespace="payments",
        name="api",
    )
    action = ActionPlanV3(
        action_id="act-1",
        recommendation_ids=[7],
        target=target,
        operations=operations,
        constraints={},
        evidence_ref="recommendation/7",
    )
    return RawManifestPlanner().plan(action, _source(kind), snapshot, BASE_REVISION)


def _container(plan, list_name: str = "containers", name: str = "api", kind: str = "Deployment") -> dict:
    manifest = YAML(typ="safe").load(plan.files[0].content)
    return next(item for item in manifest["spec"]["template"]["spec"][list_name] if item["name"] == name)


def test_raw_increase_with_a_raised_limit() -> None:
    plan = _raw_plan(
        [
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"memory": "768Mi"}, limits={"memory": "1Gi"}),
            )
        ]
    )
    resources = _container(plan)["resources"]
    assert resources == {"requests": {"cpu": "200m", "memory": "768Mi"}, "limits": {"cpu": "300m", "memory": "1Gi"}}


def test_raw_increase_above_the_limit_fails_closed() -> None:
    """Safety: an increase never leaves the request above an untouched limit."""
    with pytest.raises(RawManifestPlanError, match="exceeds its limit"):
        _raw_plan([SetContainerResources(container="api", resources=ContainerResources(requests={"cpu": "400m"}))])


def test_raw_cpu_limit_removal() -> None:
    plan = _raw_plan(
        [
            SetContainerResources(
                container="api", resources=ContainerResources(requests={"cpu": "400m"}), remove_limits=["cpu"]
            )
        ]
    )
    resources = _container(plan)["resources"]
    assert resources == {"requests": {"cpu": "400m", "memory": "512Mi"}, "limits": {"memory": "640Mi"}}
    assert "-              cpu: 300m" in plan.diff


def test_raw_native_sidecar_request() -> None:
    plan = _raw_plan(
        [
            SetContainerResources(
                container="proxy", resources=ContainerResources(requests={"cpu": "40m"}), container_kind="sidecar"
            )
        ]
    )
    assert _container(plan, "initContainers", "proxy")["resources"] == {"requests": {"cpu": "40m"}}


def test_raw_sidecar_addressed_as_a_container_fails_closed() -> None:
    with pytest.raises(RawManifestPlanError, match="proxy"):
        _raw_plan([SetContainerResources(container="proxy", resources=ContainerResources(requests={"cpu": "40m"}))])


def test_raw_argo_rollout_request_increase() -> None:
    plan = _raw_plan(
        [SetContainerResources(container="api", resources=ContainerResources(requests={"memory": "768Mi"}))],
        content=RAW_ROLLOUT,
        kind="Rollout",
    )
    assert _container(plan)["resources"] == {"requests": {"memory": "768Mi"}}


# -- canonical quantities (wave-4 e2e: Helm values received ``memory: '24114500'``) -------------


def test_a_byte_level_proposal_is_written_in_mebibytes() -> None:
    operation = _only_operation(_recommendation(current=512 * MIB, proposed=24114500))

    assert operation.resources.requests == {"memory": "23Mi"}


def test_a_byte_level_limit_is_written_in_mebibytes_and_still_covers_the_request() -> None:
    limit = LimitChange(resource="memory", action="set", current=None, proposed=26214500, reason="below_floor")
    operation = _only_operation(_recommendation(current=512 * MIB, proposed=24114500, limit_changes=[limit]))

    assert operation.resources.requests == {"memory": "23Mi"}
    assert operation.resources.limits == {"memory": "26Mi"}


def test_the_raw_planner_diff_shows_mebibytes_not_bytes() -> None:
    operation = _only_operation(_recommendation(current=512 * MIB, proposed=24114500))
    plan = _raw_plan([operation])

    assert _container(plan)["resources"]["requests"]["memory"] == "23Mi"
    assert "24114500" not in (plan.diff or "")
