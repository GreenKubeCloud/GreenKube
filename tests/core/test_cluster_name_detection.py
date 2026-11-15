"""Auto-detected CLUSTER_NAME must not depend on the node a pod runs on.

On a multi-node cluster without config.clusterName (kind, kubeadm, k3s, most
managed clusters), the name used to be the node of each pod (K8S_NODE_NAME):
the api and the controller could disagree, and a rescheduled controller wrote
under a new name (a second collection watermark and ledger cluster identity),
seen on a kind + Cilium cluster in the e2e run.
"""

from unittest.mock import patch

import pytest

from greenkube.core.config import Config


def _node(name, labels=None):
    return {"metadata": {"name": name, "labels": labels or {}}}


@pytest.fixture(autouse=True)
def _node_env(monkeypatch):
    monkeypatch.setenv("K8S_NODE_NAME", "kind-worker2")


def _detect(nodes):
    with patch.object(Config, "_list_nodes_in_cluster", return_value=nodes):
        return Config._auto_detect_cluster_name()


def test_multi_node_cluster_does_not_use_the_pod_node():
    nodes = [_node("kind-control-plane"), _node("kind-worker")]
    assert _detect((nodes, True)) == "default"
    # Every pod gets the same name, whatever node it runs on.
    with patch.dict("os.environ", {"K8S_NODE_NAME": "kind-worker"}):
        assert _detect((nodes, True)) == "default"


def test_single_node_cluster_keeps_its_node_name():
    assert _detect(([_node("minikube")], False)) == "minikube"


def test_cluster_name_label_wins():
    nodes = [_node("ip-10-0-0-1", {"eks.amazonaws.com/cluster-name": "prod-eu"}), _node("ip-10-0-0-2")]
    assert _detect((nodes, False)) == "prod-eu"


def test_failing_node_list_never_returns_the_pod_node():
    """COL-02 acceptance a): a failed or slow node list at start must not fork the cluster key."""
    assert _detect(None) == "default"


def test_without_api_access_or_node_name_is_default(monkeypatch):
    monkeypatch.delenv("K8S_NODE_NAME")
    assert _detect(None) == "default"


def test_out_of_cluster_lists_no_nodes(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert Config._list_nodes_in_cluster() is None


def test_in_cluster_listing_reads_two_nodes(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(Config, "_SERVICE_ACCOUNT_DIR", str(tmp_path))

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"metadata": {"continue": "x"}, "items": [_node("a"), _node("b")]}

    calls = {}

    def _get(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return _Response()

    with patch("httpx.get", _get):
        listing = Config._list_nodes_in_cluster()
    assert listing is not None
    items, more = listing
    assert [item["metadata"]["name"] for item in items] == ["a", "b"]
    assert more is True
    assert calls["url"] == "https://10.96.0.1:443/api/v1/nodes"
    assert calls["params"] == {"limit": "2"}
    assert calls["headers"] == {"Authorization": "Bearer abc"}
    assert calls["timeout"] == 5.0


def test_failed_listing_is_none(monkeypatch, tmp_path):
    (tmp_path / "token").write_text("abc")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setattr(Config, "_SERVICE_ACCOUNT_DIR", str(tmp_path))

    def _boom(*_args, **_kwargs):
        raise OSError("refused")

    with patch("httpx.get", _boom):
        assert Config._list_nodes_in_cluster() is None
