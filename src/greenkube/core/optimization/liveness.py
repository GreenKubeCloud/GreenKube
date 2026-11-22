"""Targets that no longer run are left out of rightsizing (REC-05).

The usage rollup keeps a target's hours for the whole lookback after it was
deleted, renamed or finished. Without a liveness check every scan re-emitted
its recommendations (refreshing their TTL) until its last hour left the
lookback. Before the analyzers run, :func:`filter_live_targets` removes from
the usage snapshot:

* every ``Job`` target: one run's requests are not a workload's. Runs of a
  CronJob are attributed to the CronJob (COL-16); targets ``Job/<run>`` of
  rows stored before that are retired like any finished Job. A running Job,
  or a Job whose pods could not be listed, is ``not_running`` (records kept);
  only a Job confirmed finished (pods listed, none running) is ``gone``;
* containers no longer in the pod spec of a running target;
* targets without a running pod whose last sample is older than
  :data:`TARGET_MAX_STALENESS` before the scan, and bare pods without a
  running pod.

A ``Deployment/<x>`` target of stored rows whose pods belong to an Argo Rollout
(the owner was cut from the ReplicaSet name before COL-16) has no running pod
and no Deployment scaled to zero, so it is retired as ``gone``. A Rollout or
bare ReplicaSet scaled to zero, and a CronJob that still exists (suspended or
between two runs), are ``not_running``; a CronJob absent from the CronJob list
is ``gone``.

A removed target is reported ``gone`` (its records are retired with the
reason ``target_gone``) only when the cluster confirms it: the pod list was
read and, for a controller, it is not present scaled to zero. A controller
scaled to zero, a DaemonSet with no scheduled pod (its node selector matches
no node right now), or a target whose liveness could not be read (Kubernetes
API unavailable), is reported ``not_running``: nothing is emitted for it and its
existing records are kept (fail closed: never retire advice on a guess).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Set, Tuple

from greenkube.models.workload_usage import POD_LEVEL, TargetKey, UsageSnapshot

logger = logging.getLogger(__name__)

#: A target without a running pod whose last sample is older than this before the scan no longer runs
#: (default of ``RECOMMENDATION_TARGET_MAX_STALENESS_HOURS``).
TARGET_MAX_STALENESS = timedelta(hours=2)
#: Target kinds never rightsized from their own usage (one run of a CronJob, a one-off Job).
EPHEMERAL_KINDS = frozenset({"Job"})
#: Controllers that may be scaled to zero while they still exist (Rollout, bare ReplicaSet: COL-16 review).
SCALABLE_KINDS = ("Deployment", "StatefulSet", "ReplicaSet", "Rollout")
#: Targets whose existence is checked against the cluster before they are retired: the scalable
#: controllers, and CronJobs, which run no pod between two runs (present or suspended: not_running).
CONTROLLER_CHECK_KINDS = (*SCALABLE_KINDS, "CronJob")
#: Controllers that may run no pod while they exist (no node matches their selector): never retired.
PODLESS_KINDS = frozenset({"DaemonSet"})

ContainerKey = Tuple[TargetKey, str]


@dataclass
class TargetLiveness:
    """What :func:`filter_live_targets` removed from a snapshot, and why."""

    #: Targets confirmed gone: their records are retired with the reason ``target_gone``.
    gone: Set[TargetKey] = field(default_factory=set)
    #: Targets not running but present (scaled to zero) or of unknown liveness: records kept.
    not_running: Set[TargetKey] = field(default_factory=set)
    #: Containers of running targets that are no longer in the pod spec.
    removed_containers: Set[ContainerKey] = field(default_factory=set)


def _last_hour(containers: dict) -> Optional[datetime]:
    hours = [summary.last_hour for summary in containers.values() if summary.last_hour is not None]
    if not hours:
        return None
    latest = max(hours)
    return latest if latest.tzinfo is not None else latest.replace(tzinfo=timezone.utc)


def filter_live_targets(
    snapshot: UsageSnapshot,
    *,
    end: datetime,
    live_containers: Optional[Set[ContainerKey]],
    scaled_to_zero: Optional[Set[TargetKey]],
    max_staleness: timedelta = TARGET_MAX_STALENESS,
) -> TargetLiveness:
    """Removes the targets and containers that no longer run from ``snapshot`` (see the module docstring).

    ``live_containers`` is the set of ``(target, container)`` of the running
    pods (None when the pod list could not be read); ``scaled_to_zero`` the
    controllers present with zero replicas (None when they could not be read).
    """
    report = TargetLiveness()
    live_targets = {target for target, _container in live_containers} if live_containers is not None else None
    for target in list(snapshot.summaries):
        containers = snapshot.summaries[target]
        running = live_targets is not None and target in live_targets
        if target[1] in EPHEMERAL_KINDS:
            del snapshot.summaries[target]
            # Retire only a Job confirmed finished; a running Job or an unreadable pod list keeps records.
            (report.gone if live_targets is not None and not running else report.not_running).add(target)
            continue
        if running:
            for name in list(containers):
                if name != POD_LEVEL and live_containers is not None and (target, name) not in live_containers:
                    del containers[name]
                    report.removed_containers.add((target, name))
            continue
        last = _last_hour(containers)
        stale = last is None or last + timedelta(hours=1) < end - max_staleness
        if not stale and not (target[1] == "Pod" and live_targets is not None):
            continue  # fresh data: a pod list racing a rollout, or liveness unknown
        del snapshot.summaries[target]
        if (
            live_targets is None
            or target[1] in PODLESS_KINDS
            or (target[1] in CONTROLLER_CHECK_KINDS and scaled_to_zero is None)
        ):
            report.not_running.add(target)
        elif scaled_to_zero is not None and target in scaled_to_zero:
            report.not_running.add(target)
        else:
            report.gone.add(target)
    if report.gone or report.not_running or report.removed_containers:
        logger.info(
            "Liveness: %d target(s) gone, %d not running (records kept), %d container(s) no longer in the pod spec.",
            len(report.gone),
            len(report.not_running),
            len(report.removed_containers),
        )
    return report


@dataclass
class StaticPodExclusion:
    """Static (mirror) pods :func:`exclude_static_pods` left out of a snapshot (F-04, REC-09)."""

    targets: Set[TargetKey] = field(default_factory=set)
    #: Distinct static pods behind those targets (one per control-plane component and node).
    pod_count: int = 0


def exclude_static_pods(snapshot: UsageSnapshot) -> StaticPodExclusion:
    """Removes the targets of static pods (owner kind ``Node``) from ``snapshot``.

    The mirror pod of a static pod (etcd, kube-apiserver, scheduler and
    controller-manager on kubeadm, kind, minikube or k3s) is owned by its Node,
    so the rollup merges a node's static pods into one target ``Node/<node>``.
    Its manifests live in the node's manifest directory, never in Git, and the
    merged statistics mix unrelated components: no analyzer may advise on it.
    The pod count of a target is its pod-level ``replicas_max`` (the static
    pods observed together in one hour), or its container count when larger.
    """
    from greenkube.core.optimization.targeting import is_static_pod_kind

    report = StaticPodExclusion()
    for target in [target for target in snapshot.summaries if is_static_pod_kind(target[1])]:
        containers = snapshot.summaries.pop(target)
        pod_level = containers.get(POD_LEVEL)
        replicas = int(getattr(pod_level, "replicas_max", 0) or 0) if pod_level is not None else 0
        named = len([name for name in containers if name != POD_LEVEL])
        report.pod_count += max(replicas, named, 1)
        report.targets.add(target)
    if report.targets:
        logger.info(
            "Static pods: %d pod(s) on %d node(s) left out of the analyzers (manifests on the node, not in Git).",
            report.pod_count,
            len(report.targets),
        )
    return report


def needs_controller_check(
    snapshot: UsageSnapshot,
    *,
    end: datetime,
    live_containers: Optional[Set[ContainerKey]],
    max_staleness: timedelta = TARGET_MAX_STALENESS,
) -> bool:
    """True when a stale scalable target or CronJob has no running pod (only then are controllers listed)."""
    if live_containers is None:
        return False
    live_targets = {target for target, _container in live_containers}
    for target, containers in snapshot.summaries.items():
        if target[1] not in CONTROLLER_CHECK_KINDS or target in live_targets:
            continue
        last = _last_hour(containers)
        if last is None or last + timedelta(hours=1) < end - max_staleness:
            return True
    return False


async def read_scaled_to_zero() -> Optional[Set[TargetKey]]:
    """Controllers that exist without running pods; None when one of the lists fails (fail closed).

    Deployments, StatefulSets, bare ReplicaSets and Argo Rollouts present with
    ``spec.replicas == 0``, and every CronJob present (suspended or between two
    runs, COL-16 review). A cluster without the Rollout CRD (HTTP 404) has no
    Rollouts; any other failed list returns None, so nothing is retired.
    """
    try:
        from greenkube.core import k8s_client

        apps = await k8s_client.get_apps_v1_api()
        batch = await k8s_client.get_batch_v1_api()
        if apps is None or batch is None:
            return None
        found: Set[TargetKey] = set()
        for kind, reader in (
            ("Deployment", apps.list_deployment_for_all_namespaces),
            ("StatefulSet", apps.list_stateful_set_for_all_namespaces),
            ("ReplicaSet", apps.list_replica_set_for_all_namespaces),
        ):
            result: Any = await reader()
            for item in getattr(result, "items", None) or []:
                metadata, spec = getattr(item, "metadata", None), getattr(item, "spec", None)
                if metadata is None or spec is None or getattr(spec, "replicas", None) != 0:
                    continue
                found.add((metadata.namespace, kind, metadata.name))
        cron_jobs: Any = await batch.list_cron_job_for_all_namespaces()
        for item in getattr(cron_jobs, "items", None) or []:
            metadata = getattr(item, "metadata", None)
            if metadata is not None:
                found.add((metadata.namespace, "CronJob", metadata.name))
        found |= await _rollouts_scaled_to_zero(k8s_client)
        return found
    except Exception as error:
        logger.warning("Liveness: could not list controllers without running pods (%s).", type(error).__name__)
        return None


async def _rollouts_scaled_to_zero(k8s_client: Any) -> Set[TargetKey]:
    """Argo Rollouts with ``spec.replicas == 0``; empty without the CRD, raises on any other failure."""
    from kubernetes_asyncio.client.rest import ApiException

    api = await k8s_client.get_custom_objects_api()
    if api is None:
        raise RuntimeError("custom objects API unavailable")
    try:
        result: Any = await api.list_cluster_custom_object(group="argoproj.io", version="v1alpha1", plural="rollouts")
    except ApiException as error:
        if error.status == 404:
            return set()
        raise
    found: Set[TargetKey] = set()
    for item in (result or {}).get("items", []) or []:
        metadata, spec = item.get("metadata") or {}, item.get("spec") or {}
        if spec.get("replicas") == 0 and metadata.get("namespace") and metadata.get("name"):
            found.add((metadata["namespace"], "Rollout", metadata["name"]))
    return found
