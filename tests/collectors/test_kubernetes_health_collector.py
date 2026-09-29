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
