"""Workload owners follow the controller chain: ReplicaSet -> Deployment/Rollout, Job -> CronJob (COL-16)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.pod_collector import PodCollector


def _ref(kind: str, name: str, controller: bool = True):
    return SimpleNamespace(kind=kind, name=name, controller=controller)


def _object(namespace: str, name: str, owners=()):
    return SimpleNamespace(metadata=SimpleNamespace(namespace=namespace, name=name, owner_references=list(owners)))


def _pod(name: str, owner_kind: str, owner_name: str):
    container = SimpleNamespace(name="app", resources=SimpleNamespace(requests={"cpu": "100m"}, limits={}))
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace="ns", owner_references=[_ref(owner_kind, owner_name)], annotations={}, uid=name
        ),
        spec=SimpleNamespace(containers=[container], init_containers=[], node_name="n1"),
    )


def _page(items):
    return SimpleNamespace(items=items, metadata=SimpleNamespace(_continue=None))


PODS = [
    _pod("web-6d5f7c9b8-abcde", "ReplicaSet", "web-6d5f7c9b8"),
    _pod("my-rs-xyz12", "ReplicaSet", "my-rs"),
    _pod("api-7f9c-qwert", "ReplicaSet", "api-7f9c"),
    _pod("report-28912345-zz9", "Job", "report-28912345"),
    _pod("once-abcde", "Job", "once"),
]
REPLICA_SETS = [
    _object("ns", "web-6d5f7c9b8", [_ref("Rollout", "web")]),
    _object("ns", "my-rs"),
    _object("ns", "api-7f9c", [_ref("Deployment", "api")]),
]
JOBS = [_object("ns", "report-28912345", [_ref("CronJob", "report")]), _object("ns", "once")]


async def _collect(replica_sets=REPLICA_SETS, jobs=JOBS, *, apps_fails=False):
    core = MagicMock()
    core.list_pod_for_all_namespaces = AsyncMock(return_value=_page(PODS))
    apps = MagicMock()
    apps.list_replica_set_for_all_namespaces = (
        AsyncMock(side_effect=RuntimeError("forbidden")) if apps_fails else AsyncMock(return_value=_page(replica_sets))
    )
    batch = MagicMock()
    batch.list_job_for_all_namespaces = AsyncMock(return_value=_page(jobs))
    with (
        patch("greenkube.collectors.pod_collector.get_core_v1_api", AsyncMock(return_value=core)),
        patch("greenkube.collectors.pod_collector.get_apps_v1_api", AsyncMock(return_value=apps)),
        patch("greenkube.collectors.pod_collector.get_batch_v1_api", AsyncMock(return_value=batch)),
    ):
        metrics = await PodCollector().collect()
    return {m.pod_name: (m.owner_kind, m.owner_name) for m in metrics}


@pytest.mark.asyncio
async def test_owners_follow_the_controller_chain():
    owners = await _collect()

    assert owners == {
        "web-6d5f7c9b8-abcde": ("Rollout", "web"),
        "my-rs-xyz12": ("ReplicaSet", "my-rs"),
        "api-7f9c-qwert": ("Deployment", "api"),
        "report-28912345-zz9": ("CronJob", "report"),
        "once-abcde": ("Job", "once"),
    }


@pytest.mark.asyncio
async def test_without_the_replicaset_list_the_name_rule_is_kept_and_logged(caplog):
    owners = await _collect(apps_fails=True)

    # The legacy rule keeps Deployment workloads stable when RBAC lacks replicasets: list.
    assert owners["api-7f9c-qwert"] == ("Deployment", "api")
    assert owners["report-28912345-zz9"] == ("CronJob", "report")
    assert "ReplicaSet owners unavailable" in caplog.text
