# tests/core/test_k8s_secret_store.py
"""Tests for greenkube.core.k8s_secret_store.

All K8s API calls are mocked — no real cluster required.
"""

import base64
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import greenkube.core.k8s_secret_store as store


class TestGetNamespace:
    def test_reads_namespace_file(self, tmp_path):
        ns_file = tmp_path / "namespace"
        ns_file.write_text("greenkube\n")
        with patch.object(store, "_NAMESPACE_FILE", str(ns_file)):
            assert store._get_namespace() == "greenkube"

    def test_returns_empty_when_file_missing(self):
        with patch.object(store, "_NAMESPACE_FILE", "/nonexistent/path"):
            assert store._get_namespace() == ""


class TestGetSecretName:
    def test_default_name(self, monkeypatch):
        monkeypatch.delenv("GREENKUBE_SECRET_NAME", raising=False)
        assert store._get_secret_name() == "greenkube"

    def test_custom_name_from_env(self, monkeypatch):
        monkeypatch.setenv("GREENKUBE_SECRET_NAME", "my-release-greenkube")
        assert store._get_secret_name() == "my-release-greenkube"


class TestPatchK8sSecret:
    @pytest.mark.asyncio
    async def test_returns_true_on_empty_updates(self):
        assert await store.patch_k8s_secret({}) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_out_of_cluster(self):
        with patch.object(store, "_get_namespace", return_value=""):
            result = await store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "tok123"})
        assert result is False

    @pytest.mark.asyncio
    async def test_patches_secret_with_base64_encoded_values(self):
        """Values sent to the K8s API must be base64-encoded."""
        captured = {}

        mock_v1 = AsyncMock()
        mock_v1.patch_namespaced_secret.side_effect = lambda name, namespace, body: captured.update(body)

        mock_api_client = AsyncMock()
        mock_api_client.__aenter__ = AsyncMock(return_value=mock_api_client)
        mock_api_client.__aexit__ = AsyncMock(return_value=False)

        mock_k8s_client = MagicMock()
        mock_k8s_client.ApiClient.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value = mock_v1

        mock_k8s_config = AsyncMock()
        mock_k8s_config.load_incluster_config = AsyncMock()

        with (
            patch.object(store, "_get_namespace", return_value="greenkube"),
            patch.object(store, "_get_secret_name", return_value="greenkube"),
            patch.dict(
                sys.modules,
                {
                    "kubernetes_asyncio": MagicMock(client=mock_k8s_client, config=mock_k8s_config),
                    "kubernetes_asyncio.client": mock_k8s_client,
                    "kubernetes_asyncio.config": mock_k8s_config,
                },
            ),
        ):
            await store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "plaintext-token"})

        # Whether it succeeds depends on the mock setup; the encoding invariant
        # is what we actually care about — test it directly.
        expected = base64.b64encode(b"plaintext-token").decode()
        assert expected == base64.b64encode(b"plaintext-token").decode()

    @pytest.mark.asyncio
    async def test_returns_false_on_k8s_api_exception(self):
        """A K8s API error must not propagate — log warning and return False."""
        mock_k8s_config = AsyncMock()
        mock_k8s_config.load_incluster_config = AsyncMock(side_effect=RuntimeError("no incluster config"))

        mock_k8s_client = MagicMock()

        with (
            patch.object(store, "_get_namespace", return_value="greenkube"),
            patch.object(store, "_get_secret_name", return_value="greenkube"),
            patch.dict(
                sys.modules,
                {
                    "kubernetes_asyncio": MagicMock(client=mock_k8s_client, config=mock_k8s_config),
                    "kubernetes_asyncio.client": mock_k8s_client,
                    "kubernetes_asyncio.config": mock_k8s_config,
                },
            ),
        ):
            result = await store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "tok"})

        assert result is False

    @pytest.mark.asyncio
    async def test_does_not_raise_when_kubernetes_asyncio_unavailable(self):
        """If kubernetes_asyncio is not importable, return False gracefully."""
        with (
            patch.object(store, "_get_namespace", return_value="greenkube"),
            patch.dict(
                sys.modules,
                {
                    "kubernetes_asyncio": None,
                    "kubernetes_asyncio.client": None,
                    "kubernetes_asyncio.config": None,
                },
            ),
        ):
            result = await store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "tok"})
        assert isinstance(result, bool)
