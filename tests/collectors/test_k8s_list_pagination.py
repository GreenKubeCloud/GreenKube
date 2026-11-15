"""Pod and node lists are paginated (WP-01.1 review 5): one unbounded list times out on large clusters."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import models as k8s

from greenkube.collectors.node_collector import NodeCollector
from greenkube.collectors.pod_collector import PodCollector


def _pod(name: str, uid: str) -> k8s.V1Pod:
    container = k8s.V1Container(name="app", resources=k8s.V1ResourceRequirements(requests={"cpu": "100m"}))
    return k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name=name, namespace="ns", uid=uid),
        spec=k8s.V1PodSpec(containers=[container]),
    )


@pytest.mark.asyncio
@patch("greenkube.collectors.pod_collector.get_core_v1_api")
async def test_pod_collector_follows_continue_tokens_and_records_uids(mock_get_api):
    api = MagicMock()
    pages = [
        k8s.V1PodList(items=[_pod("a", "uid-a")], metadata=k8s.V1ListMeta(_continue="token-1")),
        k8s.V1PodList(items=[_pod("b", "uid-b")], metadata=k8s.V1ListMeta(_continue=None)),
    ]
    api.list_pod_for_all_namespaces = AsyncMock(side_effect=pages)
    mock_get_api.return_value = api

    pods = await PodCollector().collect(raise_on_error=True)

    assert [(p.pod_name, p.pod_uid) for p in pods] == [("a", "uid-a"), ("b", "uid-b")]
    calls = api.list_pod_for_all_namespaces.await_args_list
    assert len(calls) == 2
    assert all(call.kwargs["limit"] > 0 for call in calls)
    assert "_continue" not in calls[0].kwargs
    assert calls[1].kwargs["_continue"] == "token-1"


@pytest.mark.asyncio
async def test_node_collector_follows_continue_tokens():
    def node(name: str) -> k8s.V1Node:
        return k8s.V1Node(metadata=k8s.V1ObjectMeta(name=name, labels={}), status=k8s.V1NodeStatus(capacity={}))

    api = MagicMock()
    api.list_node = AsyncMock(
        side_effect=[
            k8s.V1NodeList(items=[node("n1")], metadata=k8s.V1ListMeta(_continue="t")),
            k8s.V1NodeList(items=[node("n2")], metadata=k8s.V1ListMeta()),
        ]
    )
    collector = NodeCollector()
    collector._ensure_client = AsyncMock(return_value=api)  # type: ignore[method-assign]

    nodes = await collector.collect(raise_on_error=True)

    assert sorted(nodes) == ["n1", "n2"]
    assert api.list_node.await_args_list[1].kwargs["_continue"] == "t"
