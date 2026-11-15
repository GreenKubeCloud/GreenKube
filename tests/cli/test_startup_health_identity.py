"""Review of COL-02: a cluster identity mismatch makes the controller not ready, with its reason."""

from __future__ import annotations

from greenkube.cli.start import _StartupHealth
from greenkube.core import cluster_identity


class _Registry:
    def ready(self) -> bool:
        return True

    def snapshot(self) -> dict:
        return {"ready": True, "loops": {}}


def test_identity_mismatch_makes_the_controller_not_ready():
    health = _StartupHealth(_Registry())
    health.mark_started()
    assert health.ready()

    cluster_identity._PROBLEM["identity"] = "This database belongs to cluster a"
    try:
        assert not health.ready()
        snapshot = health.snapshot()
        assert snapshot["ready"] is False
        assert snapshot["identity_error"] == "This database belongs to cluster a"
    finally:
        cluster_identity.forget_installation_identity()

    assert health.ready()
