"""Tests for Kubernetes health observations."""

from types import SimpleNamespace

import pytest

from greenkube.collectors.kubernetes_health_collector import KubernetesHealthCollector


def _pod(ready: bool, restarts: int = 0, oom: bool = False):
    terminated = SimpleNamespace(reason="OOMKilled" if oom else "Error")
    status = SimpleNamespace(ready=ready, restart_count=restarts, last_state=SimpleNamespace(terminated=terminated))
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace="prod", labels={"app.kubernetes.io/name": "api"}),
        status=SimpleNamespace(container_statuses=[status]),
    )


class FakeApi:
    async def list_namespaced_pod(self, **kwargs):
        return SimpleNamespace(items=[_pod(True, 1), _pod(False, 2, oom=True)])


@pytest.mark.asyncio
async def test_collects_readiness_restarts_and_ooms():
    observation = await KubernetesHealthCollector(FakeApi()).collect(namespace="prod", workload="api")

    assert observation.total_pods == 2
    assert observation.ready_pods == 1
    assert observation.readiness_ratio == 0.5
    assert observation.restart_count == 3
    assert observation.oom_kill_count == 1


@pytest.mark.asyncio
async def test_container_target_health_counts_only_the_selected_container():
    web = SimpleNamespace(
        name="web",
        ready=True,
        restart_count=1,
        last_state=SimpleNamespace(terminated=SimpleNamespace(reason="Error")),
    )
    sidecar = SimpleNamespace(
        name="telemetry-sidecar",
        ready=True,
        restart_count=9,
        last_state=SimpleNamespace(terminated=SimpleNamespace(reason="OOMKilled")),
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(namespace="prod", labels={"app.kubernetes.io/name": "api"}),
        status=SimpleNamespace(container_statuses=[web, sidecar]),
    )

    class MultiContainerApi:
        async def list_namespaced_pod(self, **kwargs):
            return SimpleNamespace(items=[pod])

    observation = await KubernetesHealthCollector(MultiContainerApi()).collect(
        namespace="prod",
        workload="api",
        container_name="web",
    )

    assert observation.restart_count == 1
    assert observation.oom_kill_count == 0
    assert observation.readiness_ratio == 1.0
