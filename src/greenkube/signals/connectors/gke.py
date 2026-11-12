"""GKE recommender insights (roadmap G7).

Google's Active Assist reports GKE workloads whose CPU or memory requests are
too high or too low as **insights** of the Recommender API, subtypes
``WORKLOAD_OVERPROVISIONED`` and ``WORKLOAD_UNDERPROVISIONED``. This source
reads them and emits signals only; it never writes to the cluster or to Google
Cloud.

* **Insight type discovery.** The insight type id is not stable in public
  documentation (``google.container.DiagnosisInsight`` today), so the source
  lists the insight types at runtime (``GET v1beta1/insightTypes``) and reads
  every ``google.container.*`` type. ``insight_types`` pins them instead.
* **Insights.** ``GET v1/projects/{project}/locations/{location}/insightTypes/{type}/insights``
  for the cluster's location (and its region when the cluster is zonal), paged;
  only the two workload subtypes in state ``ACTIVE`` are used.
* **Mapping.** ``targetResources`` name the workload
  (``//container.googleapis.com/projects/P/locations/L/clusters/C/k8s/namespaces/NS/apps/deployments/NAME``).
  Insights about another cluster are skipped (``other_cluster``): advice is never
  attributed to a same-named workload elsewhere. When the insight content names
  containers and recommended requests, each becomes a ``ContainerResourcesProposal``
  (``RIGHTSIZING_CPU``/``RIGHTSIZING_MEMORY``, increases included). Otherwise the
  insight is an ``AdvisoryProposal`` (kind ``WORKLOAD_OVERPROVISIONED`` or
  ``WORKLOAD_UNDERPROVISIONED``) that stays a signal: GreenKube never invents
  the values. The content of ``google.container.DiagnosisInsight`` is not
  publicly documented, so it is read defensively (:func:`container_proposals`).
* **Gaps.** GKE gives no recommendation for workloads whose requests VPA
  manages, nor for the target metric of an HPA, and waits up to three days for
  new workloads. Every batch says so in a diagnostic (``gke_coverage``), so an
  absent insight is never read as "nothing to change".

Authentication uses the GKE metadata server (Workload Identity): the source's
Kubernetes ServiceAccount is bound to a Google service account with read
access to Recommender insights. The project, cluster name and location come
from the metadata server unless set. See ``docs/sources/gke.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from typing import Any, ClassVar, Dict, FrozenSet, List, Mapping, Optional, Tuple

import httpx
from pydantic import Field

from greenkube.signals.model import (
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
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

GKE_SOURCE_ID = "gke"
METADATA_URL = "http://metadata.google.internal"
#: The metadata server answers locally: a slow answer means it is not there.
METADATA_TIMEOUT_SECONDS = 3.0
RECOMMENDER_URL = "https://recommender.googleapis.com"
#: Insight subtypes about workload requests.
WORKLOAD_SUBTYPES = frozenset({"WORKLOAD_OVERPROVISIONED", "WORKLOAD_UNDERPROVISIONED"})
#: Prefix of the GKE insight types read after discovery.
GKE_INSIGHT_PREFIX = "google.container."
#: Pages read per insight type and location (the API returns up to 1,000 per page).
MAX_PAGES = 20
_KIND_BY_RESOURCE = {"cpu": "RIGHTSIZING_CPU", "memory": "RIGHTSIZING_MEMORY"}
_MIB = 1024 * 1024
#: ``//container.googleapis.com/projects/P/locations/L/clusters/C/k8s/namespaces/NS/<group>/<plural>/NAME``.
_TARGET = re.compile(
    r"^//container\.googleapis\.com/projects/(?P<project>[^/]+)/(?:locations|zones)/(?P<location>[^/]+)"
    r"/clusters/(?P<cluster>[^/]+)/k8s/namespaces/(?P<namespace>[^/]+)/(?:(?P<group>[^/]+)/)?"
    r"(?P<plural>[^/]+)/(?P<name>[^/]+)$"
)
_KIND_BY_PLURAL = {
    "deployments": "Deployment",
    "statefulsets": "StatefulSet",
    "daemonsets": "DaemonSet",
    "replicasets": "ReplicaSet",
    "jobs": "Job",
    "cronjobs": "CronJob",
    "rollouts": "Rollout",
}
#: Content keys a container list may be found under, and keys of its values.
_CONTAINER_LISTS = ("containerRecommendations", "containers", "containerResources")
_RECOMMENDED_KEYS = ("recommendedRequests", "recommendedRequest", "recommended", "target")
_CURRENT_KEYS = ("currentRequests", "currentRequest", "current", "requests")


class GkeSettings(SourceSettings):
    """``SOURCES_CONFIG["gke"]``. Everything defaults to the metadata server."""

    project_id: Optional[str] = Field(None, pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    #: GKE cluster name, as in ``targetResources``; the metadata server's ``cluster-name`` when unset.
    cluster_name: Optional[str] = Field(None, min_length=1, max_length=63)
    #: Cluster location (region or zone); the metadata server's ``cluster-location`` when unset.
    location: Optional[str] = Field(None, pattern=r"^[a-z]+-[a-z]+[0-9](-[a-z])?$|^global$")
    #: Insight types to read; discovered (``google.container.*``) when empty.
    insight_types: List[str] = Field(default_factory=list, max_length=10)
    timeout_seconds: float = Field(30.0, gt=0, le=300)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def parse_target(resource: str) -> Optional[Dict[str, str]]:
    """Fields of a GKE workload resource name, or None for another resource."""
    match = _TARGET.match(resource or "")
    if match is None:
        return None
    fields = {key: value for key, value in match.groupdict().items() if value is not None}
    kind = _KIND_BY_PLURAL.get(fields["plural"].lower())
    if kind is None:
        return None
    fields["kind"] = kind
    return fields


def _quantity(resource: str, value: Any) -> Optional[int]:
    if isinstance(value, Mapping):
        value = value.get("value", value.get("amount"))
    if value is None or value == "" or isinstance(value, bool):
        return None
    parsed = parse_cpu_quantity(value) if resource == "cpu" else parse_memory_quantity(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _first(mapping: Mapping[str, Any], keys: Tuple[str, ...]) -> Mapping[str, Any]:
    for key in keys:
        if isinstance(mapping.get(key), Mapping):
            return mapping[key]
    return {}


def container_proposals(content: Any) -> List[Tuple[str, str, Optional[int], int]]:
    """``(container, resource, current, recommended)`` the insight content states, if any.

    Reads a container list (``containerRecommendations``/``containers``/``containerResources``,
    also under ``content.recommendation``) whose items name the container
    (``name``/``containerName``) and its recommended requests
    (``recommendedRequests``/``recommended``/``target`` with ``cpu``/``memory``).
    Nothing is guessed: an item without a readable recommended value gives nothing.
    """
    content = _mapping(content)
    containers: Any = None
    for node in (content, _mapping(content.get("recommendation")), _mapping(content.get("workload"))):
        for key in _CONTAINER_LISTS:
            if isinstance(node.get(key), list):
                containers = node[key]
                break
        if containers is not None:
            break
    result: List[Tuple[str, str, Optional[int], int]] = []
    for item in containers or []:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name") or item.get("containerName")
        if not isinstance(name, str) or not name:
            continue
        recommended = _first(item, _RECOMMENDED_KEYS)
        current = _first(item, _CURRENT_KEYS)
        for resource in ("cpu", "memory"):
            value = _quantity(resource, recommended.get(resource))
            if value is None:
                continue
            result.append((name, resource, _quantity(resource, current.get(resource)), value))
    return result


def _fmt(resource: str, value: Optional[int]) -> str:
    if value is None:
        return "unset"
    return f"{value}m" if resource == "cpu" else f"{value / _MIB:.0f}Mi"


def _duration_seconds(value: Any) -> Optional[float]:
    """``"1296000s"`` (protobuf Duration) → seconds."""
    if isinstance(value, str) and value.endswith("s"):
        try:
            return float(value[:-1])
        except ValueError:
            return None
    return None


def _region(location: str) -> Optional[str]:
    """``europe-west1`` for the zone ``europe-west1-b``; None for a region."""
    parts = location.split("-")
    return "-".join(parts[:2]) if len(parts) == 3 and len(parts[2]) == 1 else None


class GkeRecommenderSource(Source):
    """GKE workload over- and under-provisioning insights from the Recommender API (roadmap G7)."""

    id: ClassVar[str] = GKE_SOURCE_ID
    display_name: ClassVar[str] = "GKE recommender"
    tier: ClassVar[SourceTier] = SourceTier.CLOUD
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    settings_model: ClassVar[type[SourceSettings]] = GkeSettings
    #: Insights refresh about daily: at most one collection per hour.
    limits: ClassVar[SourceLimits] = SourceLimits(
        probe_timeout_seconds=15.0, collect_timeout_seconds=180.0, min_interval_seconds=3600.0
    )
    description: ClassVar[str] = (
        "Google Recommender insights about over- and under-provisioned GKE workloads (Workload Identity, read only)."
    )

    def __init__(
        self,
        settings: SourceSettings,
        deps: SourceDeps,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        metadata_url: str = METADATA_URL,
        api_url: str = RECOMMENDER_URL,
    ):
        super().__init__(settings, deps)
        self.transport = transport
        self.metadata_url = metadata_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self._identity: Optional[Dict[str, str]] = None

    @property
    def options(self) -> GkeSettings:
        settings = self.settings
        return settings if isinstance(settings, GkeSettings) else GkeSettings()

    def _client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.options.timeout_seconds, transport=self.transport, follow_redirects=False, **kwargs
        )

    async def _metadata(self, client: httpx.AsyncClient, path: str) -> Optional[str]:
        try:
            response = await client.get(f"{self.metadata_url}/computeMetadata/v1/{path}")
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or response.headers.get("metadata-flavor", "").lower() != "google":
            return None
        return response.text.strip() or None

    async def _resolve_identity(self) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """``({project, cluster, location, token}, None)`` or ``(None, reason)``."""
        options = self.options
        async with httpx.AsyncClient(
            timeout=METADATA_TIMEOUT_SECONDS,
            transport=self.transport,
            follow_redirects=False,
            headers={"Metadata-Flavor": "Google"},
        ) as client:
            token_text = await self._metadata(client, "instance/service-accounts/default/token")
            if token_text is None:
                return None, "the GKE metadata server is not reachable (GreenKube does not run on GKE)"
            project = options.project_id or await self._metadata(client, "project/project-id")
            cluster = options.cluster_name or await self._metadata(client, "instance/attributes/cluster-name")
            location = options.location or await self._metadata(client, "instance/attributes/cluster-location")
        try:
            token = json.loads(token_text).get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not token:
            return None, "the metadata server returned no access token (check Workload Identity)"
        missing = [
            name for name, value in (("project", project), ("cluster", cluster), ("location", location)) if not value
        ]
        if missing:
            return None, f"set SOURCES_CONFIG.gke {', '.join(missing)}: the metadata server does not report it"
        return {"project": str(project), "cluster": str(cluster), "location": str(location), "token": token}, None

    async def discover(self) -> SourceHealth:
        if self.transport is None and not os.environ.get("KUBERNETES_SERVICE_HOST"):
            # Workload Identity only exists inside a GKE cluster: no probe elsewhere.
            return SourceHealth.unavailable("GreenKube does not run in a Kubernetes cluster")
        identity, reason = await self._resolve_identity()
        if identity is None:
            return SourceHealth.unavailable(reason or "no GKE identity")
        self._identity = identity
        return SourceHealth.available(version="v1")

    async def _get(self, client: httpx.AsyncClient, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def insight_types(self, client: httpx.AsyncClient, project: str, diagnostics: List[Diagnostic]) -> List[str]:
        """Pinned types, else the ``google.container.*`` types the API lists (runtime discovery)."""
        if self.options.insight_types:
            return list(self.options.insight_types)
        names: List[str] = []
        token: Optional[str] = None
        for _ in range(MAX_PAGES):
            params: Dict[str, Any] = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            payload = _mapping(await self._get(client, f"{self.api_url}/v1beta1/insightTypes", params))
            for item in payload.get("insightTypes") or []:
                name = str(_mapping(item).get("name") or "").rsplit("/", 1)[-1]
                if name.startswith(GKE_INSIGHT_PREFIX):
                    names.append(name)
            token = payload.get("nextPageToken") or None
            if not token:
                break
        if not names:
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="no_gke_insight_type",
                    message="the Recommender API lists no google.container.* insight type",
                )
            )
        return sorted(set(names))

    async def _insights(
        self, client: httpx.AsyncClient, project: str, location: str, insight_type: str
    ) -> Optional[List[Mapping[str, Any]]]:
        """Insights of one type and location; None when the location has none (404/400)."""
        url = f"{self.api_url}/v1/projects/{project}/locations/{location}/insightTypes/{insight_type}/insights"
        items: List[Mapping[str, Any]] = []
        token: Optional[str] = None
        for _ in range(MAX_PAGES):
            params: Dict[str, Any] = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            try:
                payload = _mapping(await self._get(client, url, params))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (400, 404):
                    return None
                raise
            items.extend(item for item in payload.get("insights") or [] if isinstance(item, Mapping))
            token = payload.get("nextPageToken") or None
            if not token:
                break
        return items

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        identity = self._identity
        if identity is None:
            identity, reason = await self._resolve_identity()
            if identity is None:
                raise RuntimeError(reason or "no GKE identity")
            self._identity = identity
        diagnostics: List[Diagnostic] = [
            Diagnostic(
                code="gke_coverage",
                message="GKE gives no recommendation for workloads whose requests VPA manages, for the target "
                "metric of an HPA, nor for workloads younger than about three days",
            )
        ]
        locations = [identity["location"]]
        region = _region(identity["location"])
        if region:
            locations.append(region)
        signals: List[SourceSignal] = []
        counts: Dict[str, int] = {}
        headers = {"Authorization": f"Bearer {identity['token']}", "X-Goog-User-Project": identity["project"]}
        async with self._client(headers=headers) as client:
            types = await self.insight_types(client, identity["project"], diagnostics)
            seen: set[str] = set()
            for insight_type in types:
                for location in locations:
                    insights = await self._insights(client, identity["project"], location, insight_type)
                    for insight in insights or []:
                        name = str(insight.get("name") or "")
                        if name in seen:
                            continue
                        seen.add(name)
                        signals.extend(self.insight_signals(insight, insight_type, identity, ctx, counts))
        messages = {
            "other_subtype": "insights of other subtypes than WORKLOAD_OVER/UNDERPROVISIONED were ignored",
            "inactive": "dismissed, accepted or succeeded insights were ignored",
            "other_cluster": "insights about another cluster than this one were skipped",
            "unknown_target": "insights without a readable workload target were skipped",
            "advisory_only": "insights without readable recommended requests are kept as advice only",
            "unchanged": "recommended requests equal to the current ones were skipped",
        }
        for code, amount in sorted(counts.items()):
            level = "warning" if code in ("other_cluster", "unknown_target") else "info"
            diagnostics.append(Diagnostic(level=level, code=code, message=messages[code], count=amount))
        if ctx.namespace is not None:
            signals = [signal for signal in signals if signal.target.namespace == ctx.namespace]
        unique = list({signal.signal_id: signal for signal in signals}.values())
        return SourceBatch(signals=unique, complete=True, diagnostics=diagnostics)

    def insight_signals(
        self,
        insight: Mapping[str, Any],
        insight_type: str,
        identity: Mapping[str, str],
        ctx: CollectContext,
        counts: Dict[str, int],
    ) -> List[SourceSignal]:
        def skip(code: str) -> List[SourceSignal]:
            counts[code] = counts.get(code, 0) + 1
            return []

        subtype = str(insight.get("insightSubtype") or "")
        if subtype not in WORKLOAD_SUBTYPES:
            return skip("other_subtype")
        state = str(_mapping(insight.get("stateInfo")).get("state") or "ACTIVE")
        if state != "ACTIVE":
            return skip("inactive")
        targets = [parse_target(str(item)) for item in insight.get("targetResources") or []]
        target_fields = next((item for item in targets if item is not None), None)
        if target_fields is None:
            return skip("unknown_target")
        if target_fields["cluster"] != identity["cluster"] or target_fields["project"] != identity["project"]:
            return skip("other_cluster")
        now = self.deps.clock()
        period = _duration_seconds(insight.get("observationPeriod"))
        window = TimeWindow(start=now - timedelta(seconds=period), end=now) if period else None
        algorithm = AlgorithmInfo(
            name="gke.recommender",
            params={"insight_type": insight_type, "insight_subtype": subtype},
            window=window,
        )
        kind, namespace, name = target_fields["kind"], target_fields["namespace"], target_fields["name"]
        label = f"{kind} {namespace}/{name}"
        description = str(insight.get("description") or "")[:500]
        data: Dict[str, Any] = {
            "insight": insight.get("name"),
            "insight_subtype": subtype,
            "severity": insight.get("severity"),
            "category": insight.get("category"),
            "last_refresh_time": insight.get("lastRefreshTime"),
        }
        raw = trim_raw(
            {
                key: insight.get(key)
                for key in ("name", "insightSubtype", "targetResources", "content", "lastRefreshTime", "severity")
            }
        )
        common: Dict[str, Any] = {
            "source_id": GKE_SOURCE_ID,
            "source_version": insight_type,
            "source_ref": str(insight.get("name") or "")[-512:] or None,
            "algorithm": algorithm,
            "observed_window": window,
            "provenance": SignalProvenance(origin=SignalOrigin.ADAPTER),
            "raw": raw,
            "fetched_at": now,
        }
        proposals = container_proposals(insight.get("content"))
        if not proposals:
            counts["advisory_only"] = counts.get("advisory_only", 0) + 1
            direction = "decrease" if subtype == "WORKLOAD_OVERPROVISIONED" else "increase"
            text = f"GKE reports this workload as {subtype.split('_', 1)[1].lower()}: {direction} its requests."
            return [
                SourceSignal(
                    target=TargetRef.for_kind(ctx.cluster_uid, kind, name, namespace=namespace),
                    capability=Capability.CONTAINER_REQUESTS,
                    kind=subtype,
                    proposal=AdvisoryProposal(text=text),
                    evidence=SignalEvidence(summary=f"GKE ({subtype}) on {label}. {description}".strip(), data=data),
                    **common,
                )
            ]
        signals: List[SourceSignal] = []
        for container, resource, current, recommended in proposals:
            if current is not None and current == recommended:
                counts["unchanged"] = counts.get("unchanged", 0) + 1
                continue
            if resource == "cpu":
                current_spec, proposed_spec = (
                    ResourceSpec(cpu_request_m=current),
                    ResourceSpec(cpu_request_m=recommended),
                )
            else:
                current_spec = ResourceSpec(memory_request_bytes=current)
                proposed_spec = ResourceSpec(memory_request_bytes=recommended)
            proposal = ContainerResourcesProposal(container=container, current=current_spec, proposed=proposed_spec)
            verb = {"decrease": "reduce", "increase": "raise"}.get(proposal.direction, "set")
            signals.append(
                SourceSignal(
                    target=TargetRef.for_kind(ctx.cluster_uid, kind, name, namespace=namespace, container=container),
                    capability=Capability.CONTAINER_REQUESTS,
                    kind=_KIND_BY_RESOURCE[resource],
                    proposal=proposal,
                    evidence=SignalEvidence(
                        summary=(
                            f"GKE ({subtype}) recommends to {verb} the {resource} request of {label} container "
                            f"'{container}' from {_fmt(resource, current)} to {_fmt(resource, recommended)}."
                        ),
                        details=description,
                        data=data,
                    ),
                    **common,
                )
            )
        return signals
