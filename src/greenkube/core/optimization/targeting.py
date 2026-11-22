# src/greenkube/core/optimization/targeting.py
"""Helpers for deriving stable recommendation targets from combined metrics."""

import re
from collections import defaultdict
from typing import Collection, Dict, List, Optional, Tuple

from greenkube.models.metrics import CombinedMetric

DEPLOYMENT_POD_NAME_RE = re.compile(r"^(?P<deployment>.+)-[a-z0-9]{8,10}-[a-z0-9]{5}$")

#: Owner kind of a static pod's mirror pod (kubeadm, kind, minikube, k3s control planes, F-04).
STATIC_POD_OWNER_KIND = "Node"
#: Annotation the kubelet sets on the mirror pod of a static pod.
MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"


def is_static_pod_kind(target_kind: Optional[str]) -> bool:
    """True for the target of static (mirror) pods: their manifests live on the node, not in Git."""
    return target_kind == STATIC_POD_OWNER_KIND


def infer_deployment_target(pod_name: str) -> Tuple[str, str] | None:
    """Infer a Deployment target from the standard Deployment pod name format."""
    match = DEPLOYMENT_POD_NAME_RE.match(pod_name)
    if not match:
        return None
    return "Deployment", match.group("deployment")


def resolve_target(
    namespace: str,
    pod_name: str,
    owner_kind: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Returns the stable recommendation target of a pod: its owner, an inferred Deployment, or the pod."""
    if owner_kind and owner_name:
        return (namespace, owner_kind, owner_name)

    inferred_target = infer_deployment_target(pod_name)
    if inferred_target:
        target_kind, target_name = inferred_target
        return (namespace, target_kind, target_name)

    return (namespace, "Pod", pod_name)


#: Pod-name patterns of controllers whose pods are not named after a ReplicaSet.
_STATEFULSET_POD_RE = re.compile(r"^(?P<name>.+)-\d+$")
_DAEMONSET_POD_RE = re.compile(r"^(?P<name>.+)-[a-z0-9]{5}$")


def target_for_pod(
    namespace: str,
    pod_name: str,
    known: Collection[Tuple[str, str, str]],
    owner_kind: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> Optional[Tuple[str, str, str]]:
    """The known recommendation target a pod belongs to, or None.

    The pod's owner wins when given; otherwise the Deployment, StatefulSet and
    DaemonSet naming patterns and the bare pod are tried against ``known``
    (series from Prometheus carry no owner).
    """
    candidates: List[Tuple[str, str, str]] = []
    if owner_kind and owner_name:
        candidates.append((namespace, owner_kind, owner_name))
    inferred = infer_deployment_target(pod_name)
    if inferred:
        candidates.append((namespace, *inferred))
    for kind, pattern in (("StatefulSet", _STATEFULSET_POD_RE), ("DaemonSet", _DAEMONSET_POD_RE)):
        match = pattern.match(pod_name)
        if match:
            candidates.append((namespace, kind, match.group("name")))
    candidates.append((namespace, "Pod", pod_name))
    return next((candidate for candidate in candidates if candidate in known), None)


def target_key(metric: CombinedMetric) -> Tuple[str, str, str]:
    """Returns the stable recommendation target for a metric."""
    return resolve_target(metric.namespace, metric.pod_name, metric.owner_kind, metric.owner_name)


def group_by_recommendation_target(
    metrics: List[CombinedMetric],
) -> Dict[Tuple[str, str, str], List[CombinedMetric]]:
    """Groups metrics by stable workload owner, falling back to pod name."""
    groups: Dict[Tuple[str, str, str], List[CombinedMetric]] = defaultdict(list)
    for metric in metrics:
        groups[target_key(metric)].append(metric)
    return groups


def split_series_by_container(
    series: List[CombinedMetric],
) -> List[Tuple[str | None, List[CombinedMetric]]]:
    """Split metrics by explicit container identity, falling back only to aggregate data.

    If any usable container identities exist, unlabelled samples are excluded
    rather than being mixed into a container-level analysis.
    """
    groups: Dict[str, List[CombinedMetric]] = defaultdict(list)
    for metric in series:
        name = metric.container_name
        if name and name.strip() == name:
            groups[name].append(metric)
    if groups:
        return [(name, groups[name]) for name in sorted(groups)]
    return [(None, series)] if series else []


def scope_for_target_kind(target_kind: str) -> str:
    """Maps a Kubernetes owner kind to the persisted recommendation scope."""
    return "pod" if target_kind == "Pod" else "workload"


def target_label(target_kind: str, target_name: str) -> str:
    """Returns a compact human label for a recommendation target."""
    return f"{target_kind} '{target_name}'"


def owner_fields(target_kind: str, target_name: str) -> dict:
    """Returns owner_kind/owner_name fields for workload-scoped recommendations."""
    if target_kind == "Pod":
        return {}
    return {"owner_kind": target_kind, "owner_name": target_name}
