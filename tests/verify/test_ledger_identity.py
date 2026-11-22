"""Cluster identity reads for the savings ledger (roadmap N9): transient versus permanent fallbacks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from greenkube.verify.ledger.identity import ClusterIdentityUnavailable, read_kube_system_uid


def _api(uid: str = "abc-123") -> AsyncMock:
    api = AsyncMock()
    api.read_namespace = AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(uid=uid)))
    return api


@pytest.mark.asyncio
async def test_reads_the_kube_system_uid(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    with patch("greenkube.core.k8s_client.get_core_v1_api", AsyncMock(return_value=_api())):
        assert await read_kube_system_uid() == "abc-123"


@pytest.mark.asyncio
async def test_no_client_outside_a_cluster_falls_back_to_the_cluster_name(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with patch("greenkube.core.k8s_client.get_core_v1_api", AsyncMock(return_value=None)):
        assert await read_kube_system_uid() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [AsyncMock(return_value=None), AsyncMock(side_effect=RuntimeError("no config"))])
async def test_no_client_inside_a_pod_is_transient(monkeypatch, failure):
    """A failed in-cluster config load must not key the ledger chain by the cluster name for good."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    with patch("greenkube.core.k8s_client.get_core_v1_api", failure):
        with pytest.raises(ClusterIdentityUnavailable):
            await read_kube_system_uid()


@pytest.mark.asyncio
async def test_a_forbidden_read_falls_back_to_the_cluster_name(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    api = AsyncMock()
    api.read_namespace = AsyncMock(side_effect=type("ApiException", (Exception,), {"status": 403})())
    with patch("greenkube.core.k8s_client.get_core_v1_api", AsyncMock(return_value=api)):
        assert await read_kube_system_uid() is None
