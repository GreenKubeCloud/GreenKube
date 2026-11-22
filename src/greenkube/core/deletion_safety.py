# src/greenkube/core/deletion_safety.py
"""Shared safety rules of destructive recommendations.

A deletion (orphaned LoadBalancer or PersistentVolume) is only recommended
when GreenKube can show evidence that the resource is unused, and never for
resources that operators opted out or that run the cluster itself. The rules
here are shared by the collectors, the analyzers and the deletion action;
``docs/recommendation.md`` ("Why GreenKube thinks a resource is unused")
documents them.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Mapping, Optional

#: ``greenkube.io/ignore: "true"`` or ``greenkube.io/keep`` (any value but false) opts a resource out.
IGNORE_ANNOTATION = "greenkube.io/ignore"
KEEP_ANNOTATION = "greenkube.io/keep"
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})

#: Recommendation types that delete something (no deletion PR is automated in this release).
DELETION_TYPES = frozenset({"ORPHANED_PERSISTENT_VOLUME", "ORPHANED_LOAD_BALANCER"})

#: Namespaces that run the cluster or its platform tooling: nothing in them is ever deleted.
INFRASTRUCTURE_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "coredns",
        "kubernetes-dashboard",
        "monitoring",
        "observability",
        "logging",
        "cert-manager",
        "argocd",
        "argo-cd",
        "argo-rollouts",
        "flux-system",
        "ingress-nginx",
        "nginx-ingress",
        "traefik",
        "kong",
        "contour",
        "projectcontour",
        "envoy-gateway-system",
        "metallb-system",
        "cilium",
        "cilium-secrets",
        "calico-system",
        "calico-apiserver",
        "tigera-operator",
        "kube-flannel",
        "local-path-storage",
        "longhorn-system",
        "rook-ceph",
        "openebs",
        "velero",
        "vault",
        "external-dns",
        "external-secrets",
        "sealed-secrets",
        "kyverno",
        "gatekeeper-system",
        "keda",
        "karpenter",
        "vpa",
        "opencost",
        "kubecost",
        "greenkube",
    }
)

_INFRASTRUCTURE_PATTERNS = (
    re.compile(r"^kube-"),
    re.compile(r"-system$"),
    re.compile(r"^openshift(-|$)"),
    re.compile(r"^cattle-"),
    re.compile(r"^istio-"),
    re.compile(r"^linkerd"),
    re.compile(r"^gke-"),
    re.compile(r"^gmp-"),
    re.compile(r"^aks-"),
    re.compile(r"^amazon-"),
    re.compile(r"-operators?$"),
)


def opt_out_reason(metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The opt-out annotation that protects an object, or None."""
    annotations = (metadata or {}).get("annotations") or {}
    ignore = str(annotations.get(IGNORE_ANNOTATION, "")).strip().lower()
    if ignore == "true":
        return f'annotation {IGNORE_ANNOTATION}="true"'
    if KEEP_ANNOTATION in annotations:
        keep = str(annotations.get(KEEP_ANNOTATION, "")).strip().lower()
        if keep not in _FALSE_VALUES:
            return f"annotation {KEEP_ANNOTATION}"
    return None


def is_infrastructure_namespace(namespace: Optional[str]) -> bool:
    """True for namespaces that run the cluster or its platform tooling."""
    if not namespace:
        return False
    name = namespace.strip().lower()
    return name in INFRASTRUCTURE_NAMESPACES or any(pattern.search(name) for pattern in _INFRASTRUCTURE_PATTERNS)


def min_unused_period(config: Any) -> timedelta:
    """How long a resource must stay unused before a deletion is recommended."""
    days = getattr(config, "DELETION_MIN_UNUSED_DAYS", 7.0)
    try:
        days = float(days)
    except (TypeError, ValueError):
        days = 7.0
    return timedelta(days=max(days, 0.0))


def format_days(period: timedelta) -> str:
    days = period.total_seconds() / 86400
    if days >= 1:
        return f"{days:.1f} days"
    return f"{period.total_seconds() / 3600:.1f} hours"
