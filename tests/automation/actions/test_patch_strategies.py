"""Every planner is a ``PatchStrategy``: it names its renderer and says which primitives it can express."""

from __future__ import annotations

from typing import Any

import pytest

from greenkube.automation.actions import CONTAINER_RESOURCES, CreateResource, PatchStrategy, RemoveField, SetField
from greenkube.gitops.contracts import ActionPlanV3, RendererKind
from greenkube.gitops.planning.helm import FluxHelmReleasePlanner, HelmValuesPlanner
from greenkube.gitops.planning.kustomize import KustomizePlanner
from greenkube.gitops.planning.raw_manifest import RawManifestPlanner
from tests.automation.actions.noop_kind import MIN_READY_SECONDS, SetMinReadySeconds  # pyrefly: ignore[missing-import]
from tests.gitops.planning import test_raw_manifest as raw_cases  # pyrefly: ignore[missing-import]

#: The planners render nothing here; ``supports`` never touches the renderer.
_RENDERER: Any = object()
_MAPPING = {"file_path": "apps/prod/values.yaml", "container": "api", "path": ["app", "resources"]}


def _strategies() -> list[PatchStrategy]:
    renderer = _RENDERER
    return [
        RawManifestPlanner(),
        KustomizePlanner(renderer=renderer),
        HelmValuesPlanner(renderer=renderer),
        FluxHelmReleasePlanner(renderer=renderer),
    ]


def _container_primitives(action: ActionPlanV3) -> list[Any]:
    return CONTAINER_RESOURCES.compile(action.target, action.operations)


def _noop_primitive(action: ActionPlanV3) -> SetField:
    (primitive,) = MIN_READY_SECONDS.compile(action.target, [SetMinReadySeconds(seconds=30)])
    return primitive


def _create(action: ActionPlanV3) -> CreateResource:
    return CreateResource(
        manifest={
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": action.target.name, "namespace": action.target.namespace},
        }
    )


def test_every_planner_satisfies_the_patch_strategy_protocol() -> None:
    strategies = _strategies()

    assert [strategy.renderer_kind for strategy in strategies] == [
        RendererKind.RAW,
        RendererKind.KUSTOMIZE,
        RendererKind.HELM_VALUES,
        RendererKind.HELM_RELEASE,
    ]
    for strategy in strategies:
        assert callable(strategy.supports)
        assert callable(strategy.plan)


@pytest.mark.parametrize("strategy", _strategies(), ids=lambda strategy: strategy.renderer_kind.value)
def test_every_strategy_supports_container_resource_fields(strategy: PatchStrategy) -> None:
    action = raw_cases._action()
    source = raw_cases._source(renderer=strategy.renderer_kind, renderer_config={"values_mapping": _MAPPING})

    primitives = _container_primitives(action)
    assert primitives
    assert all(strategy.supports(primitive, source).ok for primitive in primitives)


@pytest.mark.parametrize("strategy", _strategies(), ids=lambda strategy: strategy.renderer_kind.value)
def test_no_strategy_creates_resources_yet(strategy: PatchStrategy) -> None:
    action = raw_cases._action()
    source = raw_cases._source(renderer=strategy.renderer_kind)

    support = strategy.supports(_create(action), source)

    assert support.state == "unsupported"
    assert "'create'" in (support.reason or "")


@pytest.mark.parametrize(
    "strategy",
    [HelmValuesPlanner(renderer=_RENDERER), FluxHelmReleasePlanner(renderer=_RENDERER)],
    ids=["helm_values", "helm_release"],
)
def test_helm_strategies_refuse_fields_other_than_container_resources(strategy: PatchStrategy) -> None:
    action = raw_cases._action()
    source = raw_cases._source(renderer=strategy.renderer_kind, renderer_config={"values_mapping": _MAPPING})

    noop = strategy.supports(_noop_primitive(action), source)
    removal = strategy.supports(RemoveField(ref=action.target, path="/spec/minReadySeconds"), source)

    assert noop.state == "unsupported"
    assert "maps container resources only" in (noop.reason or "")
    assert removal.state == "unsupported"
    assert "'remove'" in (removal.reason or "")


def test_raw_and_kustomize_strategies_support_the_test_only_kind() -> None:
    action = raw_cases._action()
    primitive = _noop_primitive(action)

    assert RawManifestPlanner().supports(primitive, raw_cases._source()).ok
    assert (
        KustomizePlanner(renderer=_RENDERER).supports(primitive, raw_cases._source(renderer=RendererKind.KUSTOMIZE)).ok
    )


def test_helm_values_needs_a_mapping_for_the_container() -> None:
    planner = HelmValuesPlanner(renderer=_RENDERER)
    action = raw_cases._action()
    (primitive, *_) = _container_primitives(action)

    unmapped = planner.supports(primitive, raw_cases._source(renderer=RendererKind.HELM_VALUES))
    other_container = planner.supports(
        primitive,
        raw_cases._source(
            renderer=RendererKind.HELM_VALUES,
            renderer_config={"values_mapping": {**_MAPPING, "container": "worker"}},
        ),
    )

    assert unmapped.state == "needs_mapping"
    assert "'api'" in (unmapped.reason or "")
    assert other_container.state == "unsupported"
