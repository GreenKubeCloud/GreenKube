# tests/core/test_k8s_secret_store.py
"""Tests for greenkube.core.k8s_secret_store (read-only since roadmap A13)."""

import pytest

import greenkube.core.k8s_secret_store as store


def test_reads_a_mounted_secret(tmp_path):
    (tmp_path / "PROMETHEUS_URL").write_text("http://prometheus:9090\n", encoding="utf-8")

    assert store.ExternalSecretStore(str(tmp_path)).read("PROMETHEUS_URL") == "http://prometheus:9090"


def test_missing_secret_reads_as_none(tmp_path):
    assert store.ExternalSecretStore(str(tmp_path)).read("ELECTRICITY_MAPS_TOKEN") is None


def test_unreadable_secret_fails_fast(tmp_path):
    (tmp_path / "TOKEN").mkdir()

    with pytest.raises(RuntimeError):
        store.ExternalSecretStore(str(tmp_path)).read("TOKEN")


def test_module_never_writes_kubernetes_secrets():
    """BUG-60: runtime overrides were patched into the release Secret, drifting from Git."""
    assert not hasattr(store, "patch_k8s_secret")
    assert not hasattr(store, "_get_secret_name")
