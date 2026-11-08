# src/greenkube/verify/digest.py
"""Spec digests of workloads (C1).

A change is identified by the pod template it rolled out. The digest is the
SHA-256 of the canonical JSON of the *normalized* pod template:

* Kubernetes client objects are converted to plain mappings;
* ``None`` values and empty mappings or lists are dropped, so a field that is
  absent and a field that is explicitly empty hash the same;
* the ``pod-template-hash`` label (added by the ReplicaSet controller, not by
  the user) is ignored.

Every other field counts: an image bump, an environment change or a
``kubectl rollout restart`` annotation is a different spec.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from greenkube.verify.model import sha256_hex

_IGNORED_LABELS = frozenset({"pod-template-hash"})


def _plain(value: Any) -> Any:
    """Convert Kubernetes client objects to plain Python values."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, Mapping):
        return _plain(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {}
        for key, item in value.items():
            cleaned = _prune(item)
            if cleaned is None or cleaned == {} or cleaned == []:
                continue
            pruned[key] = cleaned
        return pruned
    if isinstance(value, list):
        return [_prune(item) for item in value]
    return value


def normalize_pod_template(template: Any) -> Optional[dict[str, Any]]:
    """Return the normalized pod template, or ``None`` when there is none."""
    if template is None:
        return None
    plain = _plain(template)
    if not isinstance(plain, dict):
        return None
    normalized = _prune(plain)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        labels = metadata.get("labels")
        if isinstance(labels, dict):
            kept = {key: item for key, item in labels.items() if key not in _IGNORED_LABELS}
            if kept:
                metadata["labels"] = kept
            else:
                metadata.pop("labels", None)
        if not metadata:
            normalized.pop("metadata", None)
    return normalized or None


def pod_template_digest(template: Any) -> Optional[str]:
    """SHA-256 of the normalized pod template (``None`` without a template)."""
    normalized = normalize_pod_template(template)
    return sha256_hex(normalized) if normalized is not None else None


def hpa_digest(spec: Any) -> Optional[str]:
    """SHA-256 of a HorizontalPodAutoscaler spec (min/max replicas, metrics, behavior)."""
    if spec is None:
        return None
    plain = _prune(_plain(spec))
    if not isinstance(plain, dict) or not plain:
        return None
    plain.pop("scale_target_ref", None)
    plain.pop("scaleTargetRef", None)
    return sha256_hex(plain)
