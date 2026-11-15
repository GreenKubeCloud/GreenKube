"""Targets that no longer run are left out of rightsizing (REC-05).

The usage rollup keeps a target's hours for the whole lookback after it was
deleted, renamed or finished. Without a liveness check every scan re-emitted
its recommendations (refreshing their TTL) until its last hour left the
lookback. Before the analyzers run, :func:`filter_live_targets` removes from
the usage snapshot:

* every ``Job`` target: one run's requests are not a workload's. CronJob runs
  are attributed to their Job (the collectors do not resolve a Job to its
  CronJob), so a CronJob gets no rightsizing at all for now. A running Job,
  or a Job whose pods could not be listed, is ``not_running`` (records kept);
  only a Job confirmed finished (pods listed, none running) is ``gone``;
* containers no longer in the pod spec of a running target;
* targets without a running pod whose last sample is older than
  :data:`TARGET_MAX_STALENESS` before the scan, and bare pods without a
  running pod.

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
#: Controllers that may be scaled to zero while they still exist.
SCALABLE_KINDS = ("Deployment", "StatefulSet")
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
            or (target[1] in SCALABLE_KINDS and scaled_to_zero is None)
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


def needs_controller_check(
    snapshot: UsageSnapshot,
    *,
    end: datetime,
    live_containers: Optional[Set[ContainerKey]],
    max_staleness: timedelta = TARGET_MAX_STALENESS,
) -> bool:
    """True when a stale scalable target has no running pod (only then are controllers listed)."""
    if live_containers is None:
        return False
    live_targets = {target for target, _container in live_containers}
    for target, containers in snapshot.summaries.items():
        if target[1] not in SCALABLE_KINDS or target in live_targets:
            continue
        last = _last_hour(containers)
        if last is None or last + timedelta(hours=1) < end - max_staleness:
            return True
    return False


async def read_scaled_to_zero() -> Optional[Set[TargetKey]]:
    """Deployments and StatefulSets present with ``spec.replicas == 0``; None when they cannot be read."""
    try:
        from greenkube.core.k8s_client import get_apps_v1_api

        api = await get_apps_v1_api()
        if api is None:
            return None
        found: Set[TargetKey] = set()
        for kind, reader in (
            ("Deployment", api.list_deployment_for_all_namespaces),
            ("StatefulSet", api.list_stateful_set_for_all_namespaces),
        ):
            result: Any = await reader()
            for item in getattr(result, "items", None) or []:
                metadata, spec = getattr(item, "metadata", None), getattr(item, "spec", None)
                if metadata is None or spec is None or getattr(spec, "replicas", None) != 0:
                    continue
                found.add((metadata.namespace, kind, metadata.name))
        return found
    except Exception as error:
        logger.warning("Liveness: could not list controllers scaled to zero (%s).", type(error).__name__)
        return None
