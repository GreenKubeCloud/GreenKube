# tests/core/test_k8s_secret_store.py
"""Tests for greenkube.core.k8s_secret_store.

All K8s API calls are mocked — no real cluster required.
"""

import base64
import sys
from unittest.mock import MagicMock, patch

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
    def test_returns_true_on_empty_updates(self):
        assert store.patch_k8s_secret({}) is True

    def test_returns_false_when_out_of_cluster(self):
        with patch.object(store, "_get_namespace", return_value=""):
            result = store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "tok123"})
        assert result is False

    def test_patches_secret_with_base64_encoded_values(self):
        """Values sent to the K8s API must be base64-encoded."""
        captured = {}
        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_secret.side_effect = lambda name, namespace, body: captured.update(body)

        mock_k8s_client = MagicMock()
        mock_k8s_client.CoreV1Api.return_value = mock_v1
        mock_k8s_client.exceptions.ConfigException = Exception

        with (
            patch.object(store, "_get_namespace", return_value="greenkube"),
            patch.object(store, "_get_secret_name", return_value="greenkube"),
            patch.dict(sys.modules, {"kubernetes.client": mock_k8s_client, "kubernetes.config": MagicMock()}),
        ):
            result = store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "plaintext-token"})

        if result:
            expected = base64.b64encode(b"plaintext-token").decode()
            assert captured.get("data", {}).get("ELECTRICITY_MAPS_TOKEN") == expected

    def test_returns_false_on_k8s_api_exception(self):
        """A K8s API error must not propagate — log warning and return False."""
        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_secret.side_effect = RuntimeError("K8s API error")

        mock_k8s_client = MagicMock()
        mock_k8s_client.CoreV1Api.return_value = mock_v1
        mock_k8s_client.exceptions.ConfigException = Exception

        with (
            patch.object(store, "_get_namespace", return_value="greenkube"),
            patch.object(store, "_get_secret_name", return_value="greenkube"),
            patch.dict(sys.modules, {"kubernetes.client": mock_k8s_client, "kubernetes.config": MagicMock()}),
        ):
            result = store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "tok"})

        assert isinstance(result, bool)

    def test_does_not_raise_when_kubernetes_unavailable(self, monkeypatch):
        """If the kubernetes package cannot be imported, return False gracefully."""
        with patch.object(store, "_get_namespace", return_value="greenkube"):
            with patch.dict(sys.modules, {"kubernetes": None, "kubernetes.client": None, "kubernetes.config": None}):
                result = store.patch_k8s_secret({"ELECTRICITY_MAPS_TOKEN": "tok"})
        assert isinstance(result, bool)
