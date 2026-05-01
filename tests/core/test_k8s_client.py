from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio import config as k8s_config

from greenkube.core import k8s_client


@pytest.fixture(autouse=True)
def reset_k8s_config_state(monkeypatch):
    monkeypatch.setattr(k8s_client, "_CONFIG_LOADED", False)


@pytest.mark.asyncio
async def test_ensure_k8s_config_returns_true_when_already_loaded(monkeypatch):
    load_incluster = MagicMock()
    monkeypatch.setattr(k8s_client, "_CONFIG_LOADED", True)
    monkeypatch.setattr(k8s_client.config, "load_incluster_config", load_incluster)

    assert await k8s_client.ensure_k8s_config() is True
    load_incluster.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_k8s_config_uses_incluster_config(monkeypatch):
    monkeypatch.setattr(k8s_client.config, "load_incluster_config", MagicMock(return_value=None))

    assert await k8s_client.ensure_k8s_config() is True
    assert k8s_client._CONFIG_LOADED is True


@pytest.mark.asyncio
async def test_ensure_k8s_config_falls_back_to_local_kubeconfig(monkeypatch):
    monkeypatch.setattr(k8s_client.config, "load_incluster_config", MagicMock(side_effect=k8s_config.ConfigException))
    monkeypatch.setattr(k8s_client.config, "load_kube_config", AsyncMock(return_value=None))

    assert await k8s_client.ensure_k8s_config() is True
    assert k8s_client._CONFIG_LOADED is True


@pytest.mark.asyncio
async def test_ensure_k8s_config_returns_false_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(k8s_client.config, "load_incluster_config", MagicMock(side_effect=k8s_config.ConfigException))
    monkeypatch.setattr(k8s_client.config, "load_kube_config", AsyncMock(side_effect=k8s_config.ConfigException))

    assert await k8s_client.ensure_k8s_config() is False


@pytest.mark.asyncio
async def test_ensure_k8s_config_handles_unexpected_loader_errors(monkeypatch):
    monkeypatch.setattr(k8s_client.config, "load_incluster_config", MagicMock(side_effect=RuntimeError("cluster")))
    monkeypatch.setattr(k8s_client.config, "load_kube_config", AsyncMock(side_effect=RuntimeError("local")))

    assert await k8s_client.ensure_k8s_config() is False


@pytest.mark.asyncio
async def test_get_core_and_autoscaling_apis_return_clients_when_configured(monkeypatch):
    core_api = MagicMock(name="core-api")
    autoscaling_api = MagicMock(name="autoscaling-api")
    monkeypatch.setattr(k8s_client, "ensure_k8s_config", AsyncMock(return_value=True))
    monkeypatch.setattr(k8s_client.client, "CoreV1Api", MagicMock(return_value=core_api))
    monkeypatch.setattr(k8s_client.client, "AutoscalingV2Api", MagicMock(return_value=autoscaling_api))

    assert await k8s_client.get_core_v1_api() is core_api
    assert await k8s_client.get_autoscaling_v2_api() is autoscaling_api


@pytest.mark.asyncio
async def test_get_core_and_autoscaling_apis_return_none_without_config(monkeypatch):
    monkeypatch.setattr(k8s_client, "ensure_k8s_config", AsyncMock(return_value=False))

    assert await k8s_client.get_core_v1_api() is None
    assert await k8s_client.get_autoscaling_v2_api() is None
