"""Container limits and native sidecars in the pod collector (roadmap A7, BUG-10)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import models as k8s

from greenkube.collectors.pod_collector import PodCollector


def _api_with(pods: list[k8s.V1Pod]) -> MagicMock:
    api = MagicMock()
    api.list_pod_for_all_namespaces = AsyncMock(return_value=k8s.V1PodList(items=pods))
    return api


def _pod() -> k8s.V1Pod:
    app = k8s.V1Container(
        name="app",
        resources=k8s.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "256Mi"},
            limits={"cpu": "1", "memory": "512Mi"},
        ),
    )
    native_sidecar = k8s.V1Container(
        name="proxy",
        restart_policy="Always",
        resources=k8s.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": "64Mi"},
            limits={"memory": "128Mi"},
        ),
    )
    classic_init = k8s.V1Container(
        name="migrate",
        resources=k8s.V1ResourceRequirements(requests={"cpu": "2", "memory": "1Gi"}),
    )
    return k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="web-0", namespace="shop"),
        spec=k8s.V1PodSpec(containers=[app], init_containers=[classic_init, native_sidecar]),
    )


@pytest.mark.asyncio
@patch("greenkube.collectors.pod_collector.get_core_v1_api")
async def test_native_sidecar_is_a_steady_state_container_with_requests_and_limits(mock_get_api):
    mock_get_api.return_value = _api_with([_pod()])

    results = await PodCollector().collect()

    by_name = {metric.container_name: metric for metric in results}
    assert set(by_name) == {"app", "proxy"}
    assert by_name["app"].kind == "container"
    assert (by_name["app"].cpu_request, by_name["app"].cpu_limit) == (250, 1000)
    assert (by_name["app"].memory_request, by_name["app"].memory_limit) == (256 * 1024**2, 512 * 1024**2)
    assert by_name["proxy"].kind == "sidecar"
    assert (by_name["proxy"].cpu_request, by_name["proxy"].cpu_limit) == (50, None)
    assert by_name["proxy"].memory_limit == 128 * 1024**2


@pytest.mark.asyncio
@patch("greenkube.collectors.pod_collector.get_core_v1_api")
async def test_classic_init_containers_are_recorded_only_on_request(mock_get_api):
    mock_get_api.return_value = _api_with([_pod()])

    results = await PodCollector().collect(include_init_containers=True)

    init = [metric for metric in results if metric.kind == "init"]
    assert [(metric.container_name, metric.cpu_request) for metric in init] == [("migrate", 2000)]
    steady = [metric for metric in results if metric.kind != "init"]
    assert sum(metric.cpu_request for metric in steady) == 300
