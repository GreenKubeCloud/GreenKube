"""Typed errors of the Kubernetes collectors (WP-01.2, COL-07)."""


class KubernetesListError(RuntimeError):
    """A Kubernetes list call failed: the result is unknown, not empty."""
