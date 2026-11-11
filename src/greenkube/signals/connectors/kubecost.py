"""Kubecost connector (roadmap G3).

Reads Kubecost's savings APIs and emits signals only:

* request sizing (``GET /model/savings/requestSizingV2`` with ``window``,
  ``algorithmCPU``, ``algorithmRAM``, ``qCPU``, ``qRAM``,
  ``targetCPUUtilization``, ``targetRAMUtilization`` and ``filter``) →
  ``ContainerResourcesProposal`` per container and resource
  (``RIGHTSIZING_CPU``/``RIGHTSIZING_MEMORY``), increases included; Kubecost's
  monthly savings go to ``source_estimate`` for comparison only;
* abandoned workloads → ``DeletionProposal`` with ``capability=cleanup``. The
  only evidence is low network traffic, so the signal says so and asks for
  review: GreenKube's deletion-safety rules (``DELETION_MIN_UNUSED_DAYS``,
  dependency evidence, ``deletion_requires_review``) still decide;
* node-group (cluster) sizing → an ``AdvisoryProposal`` about the cluster.

The API paths changed across Kubecost majors (3.x moved to the ``finops-agent``
with ClickHouse and renamed cluster sizing "Node Group Sizing"), so they are
kept in a versioned map (:data:`PATHS`). ``health()`` probes the version; the
map can be overridden per path with the ``paths`` setting. An endpoint that
answers 404 on the deployed version is reported as a diagnostic and skipped.

Licensing: Kubecost's free tier is limited to $100K of spend per 30 days
(see ``docs/sources/kubecost.md``).
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any, ClassVar, Dict, FrozenSet, List, Literal, Mapping, Optional, Tuple

import httpx
from pydantic import Field, field_validator

from greenkube.signals.model import (
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    ResourceSpec,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
    TimeWindow,
    trim_raw,
)
from greenkube.signals.sdk import (
    CollectContext,
    Diagnostic,
    Source,
    SourceBatch,
    SourceDeps,
    SourceHealth,
    SourceLimits,
    SourceSettings,
    SourceTier,
)
from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

logger = logging.getLogger(__name__)

KUBECOST_SOURCE_ID = "kubecost"
#: Service of the Kubecost Helm chart in its default namespace (probed when ``url`` is not set).
DEFAULT_URL = "http://kubecost-cost-analyzer.kubecost.svc:9090"

#: API paths per Kubecost major version. ``version`` is the probe.
PATHS: Dict[str, Dict[str, str]] = {
    "2": {
        "version": "/model/clusterInfo",
        "request_sizing": "/model/savings/requestSizingV2",
        "abandoned_workloads": "/model/savings/abandonedWorkloads",
        "node_group_sizing": "/model/savings/clusterSizingETL",
    },
    "3": {
        "version": "/model/clusterInfo",
        "request_sizing": "/model/savings/requestSizingV2",
        "abandoned_workloads": "/model/savings/abandonedWorkloads",
        "node_group_sizing": "/model/savings/nodeGroupSizing",
    },
}
DEFAULT_MAJOR = "2"

_CONTROLLER_KINDS = {
    "deployment": "Deployment",
    "statefulset": "StatefulSet",
    "daemonset": "DaemonSet",
    "replicaset": "ReplicaSet",
    "job": "Job",
    "cronjob": "CronJob",
    "rollout": "Rollout",
}
_KIND_BY_RESOURCE = {"cpu": "RIGHTSIZING_CPU", "memory": "RIGHTSIZING_MEMORY"}
_MIB = 1024 * 1024


class KubecostSettings(SourceSettings):
    """``SOURCES_CONFIG["kubecost"]``."""

    #: Kubecost API base URL (the cost-analyzer service); the default chart service when unset.
    url: Optional[str] = Field(None, pattern=r"^https?://[^\s]+$")
    #: Kubecost major version ("2" or "3"); probed when unset.
    version: Optional[Literal["2", "3"]] = None
    #: Lookback of the savings queries, in days (Kubecost recommends more than 2 days).
    window: str = Field("7d", pattern=r"^[1-9][0-9]{0,2}d$")
    algorithm_cpu: Literal["max", "quantile"] = "max"
    algorithm_ram: Literal["max", "quantile"] = "max"
    q_cpu: Optional[float] = Field(None, gt=0, le=1)
    q_ram: Optional[float] = Field(None, gt=0, le=1)
    target_cpu_utilization: float = Field(0.7, gt=0, le=1)
    target_ram_utilization: float = Field(0.7, gt=0, le=1)
    #: Kubecost filter expression passed as ``filter``.
    filter: Optional[str] = Field(None, max_length=1024)
    #: Kubecost cluster id of this cluster, required when Kubecost reports several clusters.
    cluster_id: Optional[str] = Field(None, max_length=253)
    abandoned_workloads: bool = True
    node_group_sizing: bool = True
    #: Per-endpoint path overrides (``version``, ``request_sizing``, ``abandoned_workloads``, ``node_group_sizing``).
    paths: Dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(30.0, gt=0, le=300)

    @field_validator("paths")
    @classmethod
    def _known_paths(cls, value: Dict[str, str]) -> Dict[str, str]:
        unknown = sorted(set(value) - set(PATHS[DEFAULT_MAJOR]))
        if unknown:
            raise ValueError(f"unknown path keys: {', '.join(unknown)}")
        for path in value.values():
            if not path.startswith("/"):
                raise ValueError("paths must start with '/'")
        return value


def _unwrap(payload: Any) -> Any:
    """Kubecost wraps most answers in ``{"code": 200, "data": ...}``."""
    if isinstance(payload, Mapping) and "data" in payload and ("code" in payload or len(payload) == 1):
        return payload["data"]
    return payload


def _entries(payload: Any) -> List[Mapping[str, Any]]:
    payload = _unwrap(payload)
    if isinstance(payload, Mapping):
        for key in ("Recommendations", "recommendations", "items", "workloads"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


def probe_version(payload: Any) -> Optional[str]:
    """``"2"``/``"3"`` from a version probe answer, or None."""
    data = _unwrap(payload)
    for node in (data, payload):
        if not isinstance(node, Mapping):
            continue
        for key in ("version", "kubecostVersion", "appVersion", "chartVersion"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                major = value.strip().lstrip("vV").split(".", 1)[0]
                if major.isdigit():
                    return major
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return None if math.isnan(number) or math.isinf(number) else number


def _quantity(resource: str, value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    parsed = parse_cpu_quantity(value) if resource == "cpu" else parse_memory_quantity(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _fmt(resource: str, value: Optional[int]) -> str:
    if value is None:
        return "unset"
    return f"{value}m" if resource == "cpu" else f"{value / _MIB:.0f}Mi"


def _window_days(window: str) -> int:
    return int(window[:-1])


class KubecostSource(Source):
    """Kubecost request sizing, abandoned workloads and node-group sizing (roadmap G3)."""

    id: ClassVar[str] = KUBECOST_SOURCE_ID
    display_name: ClassVar[str] = "Kubecost"
    tier: ClassVar[SourceTier] = SourceTier.COMMERCIAL
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset(
        {Capability.CONTAINER_REQUESTS, Capability.CLEANUP, Capability.NODE_POOL}
    )
    settings_model: ClassVar[type[SourceSettings]] = KubecostSettings
    #: Savings queries are heavy on Kubecost: at most one collection every 30 minutes.
    limits: ClassVar[SourceLimits] = SourceLimits(
        probe_timeout_seconds=15.0, collect_timeout_seconds=180.0, min_interval_seconds=1800.0
    )
    description: ClassVar[str] = (
        "Kubecost savings APIs: container request sizing, abandoned workloads (review only) and node-group sizing."
    )

    def __init__(
        self,
        settings: SourceSettings,
        deps: SourceDeps,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        super().__init__(settings, deps)
        self.transport = transport
        self.major: Optional[str] = None

    @property
    def options(self) -> KubecostSettings:
        settings = self.settings
        return settings if isinstance(settings, KubecostSettings) else KubecostSettings()

    @property
    def base_url(self) -> str:
        return (self.options.url or DEFAULT_URL).rstrip("/")

    def path(self, name: str, major: Optional[str] = None) -> str:
        override = self.options.paths.get(name)
        if override:
            return override
        return PATHS.get(major or self.major or DEFAULT_MAJOR, PATHS[DEFAULT_MAJOR])[name]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.options.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def _probe(self) -> SourceHealth:
        try:
            async with self._client() as client:
                response = await client.get(self.path("version", DEFAULT_MAJOR))
        except httpx.HTTPError as exc:
            return SourceHealth.unavailable(f"Kubecost is not reachable at {self.base_url} ({type(exc).__name__})")
        if response.status_code == 404:
            return SourceHealth.unavailable(f"{self.base_url} does not serve the Kubecost API")
        if response.status_code >= 400:
            return SourceHealth.unavailable(f"Kubecost answered HTTP {response.status_code} to the version probe")
        try:
            detected = probe_version(response.json())
        except ValueError:
            detected = None
        major = self.options.version or detected
        if major not in PATHS:
            self.major = DEFAULT_MAJOR
            return SourceHealth.degraded(
                f"Kubecost version {detected or 'unknown'} is not in the path map; using the {DEFAULT_MAJOR}.x paths"
            )
        self.major = major
        return SourceHealth.available(version=f"{major}.x")

    async def discover(self) -> SourceHealth:
        if not self.options.url and not os.environ.get("KUBERNETES_SERVICE_HOST"):
            # Zero config probes the chart's default service, which only resolves in a cluster.
            return SourceHealth.unavailable("set SOURCES_CONFIG.kubecost.url to the Kubecost API outside a cluster")
        return await self._probe()

    async def _get(
        self, client: httpx.AsyncClient, name: str, params: Dict[str, Any], diagnostics: List[Diagnostic]
    ) -> Tuple[Optional[Any], bool]:
        """``(payload, failed)``: a 404 is a missing endpoint (diagnostic), other errors fail the batch."""
        path = self.path(name)
        try:
            response = await client.get(path, params={key: value for key, value in params.items() if value is not None})
        except httpx.HTTPError as exc:
            diagnostics.append(
                Diagnostic(level="error", code=f"{name}_unreachable", message=f"{path}: {type(exc).__name__}")
            )
            return None, True
        if response.status_code == 404:
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="path_not_found",
                    message=f"{path} is not served by Kubecost {self.major or DEFAULT_MAJOR}.x; "
                    f"set SOURCES_CONFIG.kubecost.paths.{name} if it moved",
                )
            )
            return None, False
        if response.status_code >= 400:
            diagnostics.append(
                Diagnostic(level="error", code=f"{name}_failed", message=f"{path}: HTTP {response.status_code}")
            )
            return None, True
        try:
            return response.json(), False
        except ValueError:
            diagnostics.append(Diagnostic(level="error", code=f"{name}_invalid", message=f"{path}: not JSON"))
            return None, True

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        if self.major is None:
            await self._probe()
        options = self.options
        now = self.deps.clock()
        days = _window_days(options.window)
        window = TimeWindow(start=now - timedelta(days=days), end=now)
        diagnostics: List[Diagnostic] = []
        signals: List[SourceSignal] = []
        complete = True
        async with self._client() as client:
            payload, failed = await self._get(
                client,
                "request_sizing",
                {
                    "window": options.window,
                    "algorithmCPU": options.algorithm_cpu,
                    "algorithmRAM": options.algorithm_ram,
                    "qCPU": options.q_cpu,
                    "qRAM": options.q_ram,
                    "targetCPUUtilization": options.target_cpu_utilization,
                    "targetRAMUtilization": options.target_ram_utilization,
                    "filter": options.filter,
                },
                diagnostics,
            )
            complete = complete and not failed
            if payload is not None:
                signals.extend(self.request_sizing_signals(payload, ctx, now, window, diagnostics))
            if options.abandoned_workloads:
                payload, failed = await self._get(
                    client, "abandoned_workloads", {"days": days, "filter": options.filter}, diagnostics
                )
                complete = complete and not failed
                if payload is not None:
                    signals.extend(self.abandoned_signals(payload, ctx, now, window, diagnostics))
            if options.node_group_sizing:
                payload, failed = await self._get(client, "node_group_sizing", {"window": options.window}, diagnostics)
                complete = complete and not failed
                if payload is not None:
                    signals.extend(self.node_group_signals(payload, ctx, now, window))
        if ctx.namespace is not None:
            signals = [signal for signal in signals if signal.target.namespace == ctx.namespace]
        return SourceBatch(signals=signals, complete=complete, diagnostics=diagnostics)

    # ------------------------------------------------------------------ #
    # Mapping                                                             #
    # ------------------------------------------------------------------ #

    def _cluster_entries(
        self, entries: List[Mapping[str, Any]], key: str, diagnostics: List[Diagnostic]
    ) -> List[Mapping[str, Any]]:
        """Entries about this cluster: ``cluster_id`` when set; a single-cluster answer otherwise."""
        wanted = self.options.cluster_id
        if wanted:
            return [entry for entry in entries if str(entry.get(key) or "") in ("", wanted)]
        clusters = {str(entry.get(key)) for entry in entries if entry.get(key)}
        if len(clusters) > 1:
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="multiple_clusters",
                    message="Kubecost reports several clusters; set SOURCES_CONFIG.kubecost.cluster_id to this "
                    "cluster's id to use its advice",
                    count=len(clusters),
                )
            )
            return []
        return entries

    def _algorithm(self, name: str, window: TimeWindow, params: Dict[str, Any]) -> AlgorithmInfo:
        return AlgorithmInfo(name=name, params={k: v for k, v in params.items() if v is not None}, window=window)

    def request_sizing_signals(
        self,
        payload: Any,
        ctx: CollectContext,
        now: datetime,
        window: TimeWindow,
        diagnostics: List[Diagnostic],
    ) -> List[SourceSignal]:
        options = self.options
        algorithm = self._algorithm(
            "kubecost.request_sizing_v2",
            window,
            {
                "window": options.window,
                "algorithmCPU": options.algorithm_cpu,
                "algorithmRAM": options.algorithm_ram,
                "qCPU": options.q_cpu,
                "qRAM": options.q_ram,
                "targetCPUUtilization": options.target_cpu_utilization,
                "targetRAMUtilization": options.target_ram_utilization,
            },
        )
        signals: List[SourceSignal] = []
        skipped: Dict[str, int] = {}
        for entry in self._cluster_entries(_entries(payload), "clusterID", diagnostics):
            namespace = entry.get("namespace")
            kind = _CONTROLLER_KINDS.get(str(entry.get("controllerKind") or "").lower())
            name = entry.get("controllerName")
            container = entry.get("containerName")
            if not (isinstance(namespace, str) and namespace and isinstance(name, str) and name):
                skipped["invalid_entry"] = skipped.get("invalid_entry", 0) + 1
                continue
            if kind is None or not (isinstance(container, str) and container):
                skipped["no_controller"] = skipped.get("no_controller", 0) + 1
                continue
            recommended = _mapping(entry.get("recommendedRequest"))
            latest = _mapping(entry.get("latestKnownRequest"))
            savings = _mapping(entry.get("monthlySavings"))
            efficiency = _mapping(entry.get("currentEfficiency"))
            target = TargetRef.for_kind(ctx.cluster_uid, kind, name, namespace=namespace, container=container)
            for resource, rec_kind in _KIND_BY_RESOURCE.items():
                proposed = _quantity(resource, recommended.get(resource))
                if proposed is None:
                    continue
                current = _quantity(resource, latest.get(resource))
                if current is not None and current == proposed:
                    skipped["unchanged"] = skipped.get("unchanged", 0) + 1
                    continue
                if resource == "cpu":
                    proposal = ContainerResourcesProposal(
                        container=container,
                        current=ResourceSpec(cpu_request_m=current),
                        proposed=ResourceSpec(cpu_request_m=proposed),
                    )
                else:
                    proposal = ContainerResourcesProposal(
                        container=container,
                        current=ResourceSpec(memory_request_bytes=current),
                        proposed=ResourceSpec(memory_request_bytes=proposed),
                    )
                monthly = _number(savings.get(resource))
                verb = {"decrease": "reduce", "increase": "raise"}.get(proposal.direction, "set")
                signals.append(
                    SourceSignal(
                        source_id=KUBECOST_SOURCE_ID,
                        source_version=f"{self.major or DEFAULT_MAJOR}.x",
                        source_ref=f"{namespace}/{kind}/{name}",
                        target=target,
                        capability=Capability.CONTAINER_REQUESTS,
                        kind=rec_kind,
                        proposal=proposal,
                        algorithm=algorithm,
                        evidence=SignalEvidence(
                            summary=(
                                f"Kubecost recommends to {verb} the {resource} request of {kind} {namespace}/{name} "
                                f"container '{container}' from {_fmt(resource, current)} to "
                                f"{_fmt(resource, proposed)}."
                            ),
                            data={"efficiency": {k: v for k, v in efficiency.items() if _number(v) is not None}},
                        ),
                        observed_window=window,
                        source_estimate={"monthly_savings": monthly, "currency": "USD"}
                        if monthly is not None
                        else None,
                        provenance=SignalProvenance(origin=SignalOrigin.ADAPTER),
                        raw=trim_raw(dict(entry)),
                        fetched_at=now,
                    )
                )
        for code, count in sorted(skipped.items()):
            diagnostics.append(Diagnostic(code=code, message=f"request sizing entries skipped: {code}", count=count))
        return signals

    def abandoned_signals(
        self,
        payload: Any,
        ctx: CollectContext,
        now: datetime,
        window: TimeWindow,
        diagnostics: List[Diagnostic],
    ) -> List[SourceSignal]:
        days = _window_days(self.options.window)
        by_target: Dict[str, Tuple[TargetRef, float, float, float, List[str]]] = {}
        for entry in self._cluster_entries(_entries(payload), "clusterId", diagnostics):
            namespace = entry.get("namespace")
            pod = entry.get("pod")
            if not (isinstance(namespace, str) and namespace and isinstance(pod, str) and pod):
                continue
            owners = [owner for owner in entry.get("owners") or [] if isinstance(owner, Mapping)]
            owner = owners[0] if owners else None
            kind = _CONTROLLER_KINDS.get(str(owner.get("kind") or "").lower()) if owner else None
            owner_name = owner.get("name") if owner else None
            if kind and isinstance(owner_name, str) and owner_name:
                target = TargetRef.for_kind(ctx.cluster_uid, kind, owner_name, namespace=namespace)
            else:
                target = TargetRef.for_kind(ctx.cluster_uid, "Pod", pod, namespace=namespace)
            ingress = _number(entry.get("ingressBytesPerSecond")) or 0.0
            egress = _number(entry.get("egressBytesPerSecond")) or 0.0
            monthly = _number(entry.get("monthlySavings")) or 0.0
            known = by_target.get(target.key())
            if known is None:
                by_target[target.key()] = (target, ingress, egress, monthly, [pod])
            else:
                _t, k_in, k_out, k_monthly, pods = known
                by_target[target.key()] = (
                    target,
                    max(k_in, ingress),
                    max(k_out, egress),
                    k_monthly + monthly,
                    [*pods, pod],
                )
        algorithm = self._algorithm("kubecost.abandoned_workloads", window, {"days": days})
        signals: List[SourceSignal] = []
        for target, ingress, egress, monthly, pods in by_target.values():
            label = f"{target.kind} {target.namespace}/{target.name}"
            signals.append(
                SourceSignal(
                    source_id=KUBECOST_SOURCE_ID,
                    source_version=f"{self.major or DEFAULT_MAJOR}.x",
                    source_ref=f"{target.namespace}/{target.kind}/{target.name}",
                    target=target,
                    capability=Capability.CLEANUP,
                    kind="ABANDONED_WORKLOAD",
                    proposal=DeletionProposal(
                        reason=f"Kubecost reports {label} as abandoned (almost no network traffic over {days} days)."
                    ),
                    algorithm=algorithm,
                    evidence=SignalEvidence(
                        summary=f"{label} appears abandoned: Kubecost saw almost no network traffic over {days} days.",
                        details=(
                            "Low network traffic is the only evidence. Review before deleting: GreenKube's deletion "
                            "safety rules (minimum unused duration, dependency evidence) still apply and deletion "
                            "always requires review."
                        ),
                        data={
                            "basis": "network_traffic",
                            "ingress_bytes_per_second": ingress,
                            "egress_bytes_per_second": egress,
                            "observed_days": days,
                            "pods": pods[:20],
                            "deletion_requires_review": True,
                        },
                    ),
                    observed_window=window,
                    source_estimate={"monthly_savings": monthly, "currency": "USD"} if monthly else None,
                    provenance=SignalProvenance(origin=SignalOrigin.ADAPTER),
                    raw={"pods": len(pods)},
                    fetched_at=now,
                )
            )
        return signals

    def node_group_signals(
        self, payload: Any, ctx: CollectContext, now: datetime, window: TimeWindow
    ) -> List[SourceSignal]:
        data = _unwrap(payload)
        if not data:
            return []
        savings = None
        if isinstance(data, Mapping):
            for key in ("totalMonthlySavings", "monthlySavings", "savings"):
                savings = _number(data.get(key))
                if savings is not None:
                    break
        # The proposal text is part of the signal identity: keep figures out of it.
        text = (
            "Kubecost suggests resizing the cluster's node groups; "
            "review its node-group sizing before changing node pools."
        )
        summary = text if savings is None else f"{text} Kubecost estimates {savings:.2f} USD per month."
        return [
            SourceSignal(
                source_id=KUBECOST_SOURCE_ID,
                source_version=f"{self.major or DEFAULT_MAJOR}.x",
                target=TargetRef.for_kind(ctx.cluster_uid, "Cluster", ctx.cluster_uid),
                capability=Capability.NODE_POOL,
                kind="NODE_GROUP_SIZING",
                proposal=AdvisoryProposal(text=text),
                algorithm=self._algorithm("kubecost.node_group_sizing", window, {"window": self.options.window}),
                evidence=SignalEvidence(summary=summary, data={"risk_class": "high"}),
                observed_window=window,
                source_estimate={"monthly_savings": savings, "currency": "USD"} if savings is not None else None,
                provenance=SignalProvenance(origin=SignalOrigin.ADAPTER),
                raw=trim_raw(data if isinstance(data, dict) else {"data": data}),
                fetched_at=now,
            )
        ]
