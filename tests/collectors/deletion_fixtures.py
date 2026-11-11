# tests/collectors/deletion_fixtures.py
"""Loads the kubectl-shaped deletion-evidence fixtures into a StaticObjectReader."""

import json
from pathlib import Path

from greenkube.collectors.k8s_object_reader import StaticObjectReader

FIXTURES = Path(__file__).parent / "fixtures" / "deletion_evidence"

_PLURALS = {
    "Service": "services",
    "Endpoints": "endpoints",
    "EndpointSlice": "endpointslices",
    "Pod": "pods",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets",
    "CronJob": "cronjobs",
    "Ingress": "ingresses",
    "Gateway": "gateways",
    "HTTPRoute": "httproutes",
    "GRPCRoute": "grpcroutes",
    "PersistentVolume": "persistentvolumes",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "VolumeSnapshot": "volumesnapshots",
    "VolumeSnapshotContent": "volumesnapshotcontents",
    "Namespace": "namespaces",
}


def load_objects(*names):
    """Group the items of kubectl List fixtures by plural kind."""
    objects = {}
    for name in names:
        manifest = json.loads((FIXTURES / name).read_text())
        for item in manifest["items"]:
            objects.setdefault(_PLURALS[item["kind"]], []).append(item)
    return objects


def reader_for(*names, statuses=None, extra=None):
    objects = load_objects(*names)
    for kind, items in (extra or {}).items():
        objects.setdefault(kind, []).extend(items)
    return StaticObjectReader(objects, statuses=statuses)
