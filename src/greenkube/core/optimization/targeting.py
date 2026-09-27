# src/greenkube/core/optimization/targeting.py
"""Helpers for deriving stable recommendation targets from combined metrics."""

import re
from collections import defaultdict
from typing import Dict, List, Tuple

from greenkube.models.metrics import CombinedMetric

DEPLOYMENT_POD_NAME_RE = re.compile(r"^(?P<deployment>.+)-[a-z0-9]{8,10}-[a-z0-9]{5}$")


def infer_deployment_target(pod_name: str) -> Tuple[str, str] | None:
    """Infer a Deployment target from the standard Deployment pod name format."""
    match = DEPLOYMENT_POD_NAME_RE.match(pod_name)
    if not match:
        return None
    return "Deployment", match.group("deployment")


def target_key(metric: CombinedMetric) -> Tuple[str, str, str]:
    """Returns the stable recommendation target for a metric."""
    if metric.owner_kind and metric.owner_name:
        return (metric.namespace, metric.owner_kind, metric.owner_name)

    inferred_target = infer_deployment_target(metric.pod_name)
    if inferred_target:
        target_kind, target_name = inferred_target
        return (metric.namespace, target_kind, target_name)

    return (metric.namespace, "Pod", metric.pod_name)


def group_by_recommendation_target(
    metrics: List[CombinedMetric],
) -> Dict[Tuple[str, str, str], List[CombinedMetric]]:
    """Groups metrics by stable workload owner, falling back to pod name."""
    groups: Dict[Tuple[str, str, str], List[CombinedMetric]] = defaultdict(list)
    for metric in metrics:
        groups[target_key(metric)].append(metric)
    return groups


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
