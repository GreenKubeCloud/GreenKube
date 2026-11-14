# src/greenkube/core/reliability_signals.py
"""Reliability signals of a recommendation scan (roadmap E9 remainder, USER BUG 2).

The usage rollup holds usage and requests only. Before the analyzers run, the
scan fills each :class:`~greenkube.models.workload_usage.WorkloadUsageSummary`
with what rightsizing needs to size increases and to judge decreases:

* ``oom_kill_count``: container restarts over the lookback whose termination,
  at the 5-minute step of the restart, was ``OOMKilled`` (kube-state-metrics), or cAdvisor
  ``container_oom_events_total`` when larger; 0 when kube-state-metrics is
  scraped and reports none, None when it is not.
* ``restarts_in_window``: container restarts over the lookback, whatever their
  cause (kube-state-metrics); 0 when it is scraped and reports none. A memory
  decrease is never proposed for a container that restarted (wave-4 e2e).
* ``throttling_ratio``: throttled / total CFS periods over the lookback
  (cAdvisor; the series only exist for containers with a CPU limit).
* ``jvm_hint``: why the container is believed to run a JVM (``JAVA_TOOL_OPTIONS``
  and friends, ``-Xmx`` in its command, a JVM image name); read from the pod
  specs of the Kubernetes API.

Every source is best effort: a signal that cannot be read stays None
("unknown"), never "healthy". Duplicate series of one container (several
kubelet endpoints or scrape jobs) are merged with ``max``, like usage.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from greenkube.core.optimization.targeting import target_for_pod
from greenkube.models.workload_usage import POD_LEVEL, TargetKey, UsageSnapshot

logger = logging.getLogger(__name__)

QueryFunction = Callable[[str, datetime], Awaitable[Optional[List[Dict[str, Any]]]]]

_CONTAINERS = 'container!="",container!="POD"'
#: Resolution at which restarts are matched with their termination reason.
OOM_STEP = "5m"

#: Environment variables that configure a JVM.
JVM_ENV_VARS = frozenset(
    {
        "JAVA_TOOL_OPTIONS",
        "JAVA_OPTS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "JVM_OPTS",
        "CATALINA_OPTS",
        "ES_JAVA_OPTS",
        "OPENSEARCH_JAVA_OPTS",
        "KAFKA_HEAP_OPTS",
        "LS_JAVA_OPTS",
        "SONAR_WEB_JAVAOPTS",
    }
)
#: Image repository words of JVM runtimes and well-known JVM servers.
JVM_IMAGE_WORDS = frozenset(
    {
        "openjdk",
        "temurin",
        "corretto",
        "zulu",
        "jdk",
        "jre",
        "java",
        "graalvm",
        "tomcat",
        "jetty",
        "wildfly",
        "jboss",
        "keycloak",
        "elasticsearch",
        "opensearch",
        "logstash",
        "kafka",
        "zookeeper",
        "cassandra",
        "jenkins",
        "sonarqube",
        "nexus3",
        "solr",
        "neo4j",
        "flink",
        "spark",
        "trino",
        "activemq",
        "artemis",
    }
)
_IMAGE_SPLIT = re.compile(r"[-_.]")


def jvm_hint(image: Optional[str], env_names: Iterable[str], command: Sequence[str]) -> Optional[str]:
    """Why a container is believed to run a JVM, or None."""
    for name in env_names:
        if name in JVM_ENV_VARS:
            return name
    arguments = [str(arg) for arg in command]
    for flag in ("-Xmx", "-Xms", "-XX:MaxRAMPercentage"):
        if any(flag in arg for arg in arguments):
            return flag
    if any(arg.rsplit("/", 1)[-1] == "java" or arg.startswith("java ") for arg in arguments):
        return "java command"
    if image:
        reference = image.split("@", 1)[0]
        path, _, tag = reference.rpartition(":") if ":" in reference.rsplit("/", 1)[-1] else (reference, "", "")
        repository = (path or reference).rsplit("/", 1)[-1].lower()
        words = {repository, *_IMAGE_SPLIT.split(repository)}
        tag_words = set(_IMAGE_SPLIT.split(tag.lower())) & {"jre", "jdk"}
        matched = sorted((words & JVM_IMAGE_WORDS) | tag_words)
        if matched:
            return f"image {repository}"
    return None


@dataclass
class ReliabilitySignals:
    """Signals per target container (``""`` = whole pod), with what was readable."""

    oom_kills: Dict[Tuple[TargetKey, str], int] = field(default_factory=dict)
    restarts: Dict[Tuple[TargetKey, str], int] = field(default_factory=dict)
    throttled: Dict[Tuple[TargetKey, str], float] = field(default_factory=dict)
    periods: Dict[Tuple[TargetKey, str], float] = field(default_factory=dict)
    jvm: Dict[Tuple[TargetKey, str], str] = field(default_factory=dict)
    #: Pod-spec list of each container (H1): ``container``, ``sidecar`` or ``init``.
    container_kinds: Dict[Tuple[TargetKey, str], str] = field(default_factory=dict)
    #: kube-state-metrics answered: a target without OOM kills has 0, not unknown.
    oom_known: bool = False


def _value(item: Mapping[str, Any]) -> Optional[float]:
    value = item.get("value")
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        number = float(value[1])
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _per_container(
    results: Optional[List[Dict[str, Any]]], known: Iterable[TargetKey]
) -> Dict[Tuple[TargetKey, str], float]:
    """Sum of per-pod values per known target container (pods of one target are added up)."""
    known = set(known)
    totals: Dict[Tuple[TargetKey, str], float] = defaultdict(float)
    for item in results or []:
        metric = item.get("metric") or {}
        namespace, pod, container = metric.get("namespace"), metric.get("pod"), metric.get("container")
        value = _value(item)
        if not namespace or not pod or not container or value is None:
            continue
        target = target_for_pod(namespace, pod, known)
        if target is not None:
            totals[(target, container)] += value
    return dict(totals)


async def read_prometheus_signals(
    query: QueryFunction, snapshot: UsageSnapshot, *, window_seconds: float, at: datetime
) -> ReliabilitySignals:
    """OOM kills and CPU throttling of the snapshot's targets over ``window_seconds`` before ``at``."""
    window = f"{max(int(math.ceil(window_seconds)), 300)}s"
    known = list(snapshot.summaries)
    signals = ReliabilitySignals()

    ksm_present = await query("count(kube_pod_container_status_restarts_total)", at)
    signals.oom_known = bool(ksm_present) and any((_value(item) or 0) > 0 for item in ksm_present or [])
    # Count only the restarts of a step whose last termination, at that step, is OOMKilled:
    # a liveness-probe restart after an older OOM kill in the lookback is not an OOM kill.
    ksm_oom = await query(
        "max by (namespace, pod, container) (sum_over_time(("
        f"(round(increase(kube_pod_container_status_restarts_total[{OOM_STEP}])) > 0) "
        "and on (namespace, pod, container) "
        f'(kube_pod_container_status_last_terminated_reason{{reason="OOMKilled"}} > 0))[{window}:{OOM_STEP}]))',
        at,
    )
    cadvisor_oom = await query(
        f"max by (namespace, pod, container) (increase(container_oom_events_total{{{_CONTAINERS}}}[{window}]))",
        at,
    )
    kills: Dict[Tuple[TargetKey, str], float] = {}
    for source in (_per_container(ksm_oom, known), _per_container(cadvisor_oom, known)):
        for key, value in source.items():
            kills[key] = max(kills.get(key, 0.0), value)
    signals.oom_kills = {key: int(round(value)) for key, value in kills.items() if round(value) > 0}
    restarts = await query(
        "max by (namespace, pod, container) "
        f"(round(increase(kube_pod_container_status_restarts_total{{{_CONTAINERS}}}[{window}])))",
        at,
    )
    signals.restarts = {
        key: int(round(value)) for key, value in _per_container(restarts, known).items() if round(value) > 0
    }

    throttled = await query(
        "max by (namespace, pod, container) "
        f"(increase(container_cpu_cfs_throttled_periods_total{{{_CONTAINERS}}}[{window}]))",
        at,
    )
    periods = await query(
        f"max by (namespace, pod, container) (increase(container_cpu_cfs_periods_total{{{_CONTAINERS}}}[{window}]))",
        at,
    )
    signals.throttled = _per_container(throttled, known)
    signals.periods = _per_container(periods, known)
    return signals


def _pod_targets(pods: Iterable[Any], snapshot: UsageSnapshot) -> Iterable[Tuple[TargetKey, Any]]:
    """``(target, pod spec)`` of each pod whose target has usage summaries."""
    known = set(snapshot.summaries)
    for pod in pods:
        metadata = getattr(pod, "metadata", None)
        spec = getattr(pod, "spec", None)
        if metadata is None or spec is None:
            continue
        owner_kind = owner_name = None
        for ref in getattr(metadata, "owner_references", None) or []:
            if getattr(ref, "controller", False):
                owner_kind, owner_name = ref.kind, ref.name
                break
        if owner_kind == "ReplicaSet" and owner_name and "-" in owner_name:
            owner_kind, owner_name = "Deployment", owner_name.rsplit("-", 1)[0]
        target = target_for_pod(metadata.namespace, metadata.name, known, owner_kind, owner_name)
        if target is not None:
            yield target, spec


def container_kinds_from_pods(pods: Iterable[Any], snapshot: UsageSnapshot) -> Dict[Tuple[TargetKey, str], str]:
    """Pod-spec list of each target container (H1): ``container``, ``sidecar`` (an init container
    with ``restartPolicy: Always``) or ``init``; the first pod read wins."""
    kinds: Dict[Tuple[TargetKey, str], str] = {}
    for target, spec in _pod_targets(pods, snapshot):
        for container in getattr(spec, "containers", None) or []:
            kinds.setdefault((target, container.name), "container")
        for container in getattr(spec, "init_containers", None) or []:
            kind = "sidecar" if getattr(container, "restart_policy", None) == "Always" else "init"
            kinds.setdefault((target, container.name), kind)
    return kinds


def jvm_hints_from_pods(pods: Iterable[Any], snapshot: UsageSnapshot) -> Dict[Tuple[TargetKey, str], str]:
    """JVM hints per target container from Kubernetes pod objects (``V1Pod`` or alike)."""
    hints: Dict[Tuple[TargetKey, str], str] = {}
    for target, spec in _pod_targets(pods, snapshot):
        for container in getattr(spec, "containers", None) or []:
            env_names = [getattr(var, "name", "") for var in getattr(container, "env", None) or []]
            command = [*(getattr(container, "command", None) or []), *(getattr(container, "args", None) or [])]
            hint = jvm_hint(getattr(container, "image", None), env_names, command)
            if hint:
                hints.setdefault((target, container.name), hint)
    return hints


def _by_target(values: Mapping[Tuple[TargetKey, str], Any]) -> Dict[TargetKey, Dict[str, Any]]:
    grouped: Dict[TargetKey, Dict[str, Any]] = defaultdict(dict)
    for (target, container), value in values.items():
        grouped[target][container] = value
    return grouped


def apply_signals(snapshot: UsageSnapshot, signals: ReliabilitySignals) -> None:
    """Fills the snapshot's summaries; the pod-level summary gets the totals over its containers."""
    kills_by_target = _by_target(signals.oom_kills)
    restarts_by_target = _by_target(signals.restarts)
    throttled_by_target = _by_target(signals.throttled)
    periods_by_target = _by_target(signals.periods)
    jvm_by_target = _by_target(signals.jvm)
    for target, containers in snapshot.summaries.items():
        for name, summary in containers.items():
            if name == POD_LEVEL:
                kills = sum(kills_by_target.get(target, {}).values())
                restarts = sum(restarts_by_target.get(target, {}).values())
                throttled = sum(throttled_by_target.get(target, {}).values())
                periods = sum(periods_by_target.get(target, {}).values())
                hints = jvm_by_target.get(target, {})
                hint = hints[min(hints)] if hints else None
            else:
                kills = signals.oom_kills.get((target, name), 0)
                restarts = signals.restarts.get((target, name), 0)
                throttled = signals.throttled.get((target, name), 0.0)
                periods = signals.periods.get((target, name), 0.0)
                hint = signals.jvm.get((target, name))
            if kills or signals.oom_known:
                summary.oom_kill_count = kills
            if restarts or signals.oom_known:
                summary.restarts_in_window = restarts
            if periods > 0:
                summary.throttling_ratio = max(0.0, min(throttled / periods, 1.0))
            if hint:
                summary.jvm_hint = hint
            kind = signals.container_kinds.get((target, name)) if name != POD_LEVEL else None
            if kind:
                summary.container_kind = kind


async def attach_reliability_signals(
    snapshot: UsageSnapshot,
    config: Any,
    *,
    start: datetime,
    end: datetime,
    query: Optional[QueryFunction] = None,
    list_pods: Optional[Callable[[], Awaitable[List[Any]]]] = None,
) -> ReliabilitySignals:
    """Reads and attaches OOM kills, CPU throttling and JVM hints; never raises."""
    signals = ReliabilitySignals()
    if not snapshot.summaries:
        return signals
    if query is None and (getattr(config, "PROMETHEUS_URL", "") or "").strip():
        from greenkube.collectors.workload_health_collector import WorkloadHealthCollector

        collector = WorkloadHealthCollector(settings=config)
        query = collector._query
    if query is not None:
        try:
            signals = await read_prometheus_signals(
                query, snapshot, window_seconds=(end - start).total_seconds(), at=end
            )
        except Exception as error:  # a missing signal is unknown, never fatal to a scan
            logger.warning("Could not read OOM and CPU throttling signals: %s", error)
    try:
        pods = await (list_pods or _list_pods)()
        signals.jvm = jvm_hints_from_pods(pods, snapshot)
        signals.container_kinds = container_kinds_from_pods(pods, snapshot)
    except Exception as error:
        logger.warning("Could not read pod specs for JVM detection: %s", error)
    apply_signals(snapshot, signals)
    logger.debug(
        "Reliability signals: %d containers with OOM kills, %d with CFS data, %d JVM hints.",
        len(signals.oom_kills),
        len(signals.periods),
        len(signals.jvm),
    )
    return signals


async def _list_pods() -> List[Any]:
    from greenkube.core.config import config as global_config
    from greenkube.core.k8s_client import get_core_v1_api

    api = await get_core_v1_api()
    if api is None:
        return []
    pods = await api.list_pod_for_all_namespaces(
        watch=False, _request_timeout=getattr(global_config, "K8S_REQUEST_TIMEOUT", 30) or None
    )
    return list(pods.items or [])
