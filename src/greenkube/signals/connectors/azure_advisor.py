"""Azure Advisor recommendations for AKS clusters (roadmap G9).

Azure Advisor gives configuration-level advice about an AKS cluster (enable the
cluster autoscaler or VPA, adjust the autoscaler profile, enable cost
analysis...), never per-workload numbers. This source reads it and emits
**advisory** signals only (:class:`~greenkube.signals.model.AdvisoryProposal`
on the ``Cluster`` target): they never become pull requests.

* ``GET https://management.azure.com/subscriptions/{subscription}/providers/Microsoft.Advisor/recommendations``
  (``api-version=2023-01-01``, paged by ``nextLink``);
* only recommendations whose ``impactedField`` is
  ``Microsoft.ContainerService/managedClusters`` and whose ``impactedValue``
  (and ``resourceGroup`` when set) name this cluster are used; others are
  counted in diagnostics, never attributed to this cluster;
* ``shortDescription.problem``/``solution`` → the advisory text; ``category``
  and ``impact`` → evidence; Advisor's savings (``extendedProperties.savingsAmount``)
  → ``source_estimate`` for comparison only;
* capability ``node_pool`` for autoscaler and node-pool advice, ``hygiene``
  otherwise.

Authentication uses Azure Workload Identity: the federated token the AKS
webhook mounts (``AZURE_FEDERATED_TOKEN_FILE``, ``AZURE_CLIENT_ID``,
``AZURE_TENANT_ID``) is exchanged for an Azure Resource Manager token. The
identity needs the ``Reader`` role (or ``Advisor Reviewer``) on the
subscription or resource group; GreenKube needs no cluster write access. See
``docs/sources/azure-advisor.md``.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, ClassVar, Dict, FrozenSet, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from greenkube.signals.model import (
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
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

logger = logging.getLogger(__name__)

AZURE_ADVISOR_SOURCE_ID = "azure_advisor"
ARM_URL = "https://management.azure.com"
AUTHORITY_URL = "https://login.microsoftonline.com/"
API_VERSION = "2023-01-01"
AKS_IMPACTED_FIELD = "microsoft.containerservice/managedclusters"
#: Pages read (Advisor returns up to 100 recommendations per page).
MAX_PAGES = 50
#: Words that make an advice about node pools and autoscaling (``node_pool``) rather than hygiene.
_NODE_POOL_WORDS = ("autoscal", "node pool", "nodepool", "spot", "scale down", "scale-down", "vm size", "sku")


class AzureAdvisorSettings(SourceSettings):
    """``SOURCES_CONFIG["azure_advisor"]``."""

    subscription_id: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")
    #: AKS cluster name as Azure knows it (``impactedValue``). Required: advice is never guessed.
    cluster_name: Optional[str] = Field(None, min_length=1, max_length=63)
    #: Resource group of the cluster; narrows the match when several clusters share a name.
    resource_group: Optional[str] = Field(None, min_length=1, max_length=90)
    #: Advisor categories to read (``Cost``, ``HighAvailability``, ``OperationalExcellence``, ``Performance``,
    #: ``Security``).
    categories: List[str] = Field(
        default_factory=lambda: ["Cost", "HighAvailability", "OperationalExcellence", "Performance"]
    )
    timeout_seconds: float = Field(30.0, gt=0, le=300)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _origin(url: str) -> Tuple[str, str]:
    """Scheme and host (with port) of a URL, lowercased."""
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def capability_for(text: str) -> Capability:
    lowered = text.lower()
    return Capability.NODE_POOL if any(word in lowered for word in _NODE_POOL_WORDS) else Capability.HYGIENE


def _resource_group(resource_id: str) -> Optional[str]:
    parts = resource_id.strip("/").split("/")
    lowered = [part.lower() for part in parts]
    if "resourcegroups" in lowered:
        index = lowered.index("resourcegroups")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


class AzureAdvisorSource(Source):
    """Azure Advisor configuration advice about this AKS cluster (roadmap G9)."""

    id: ClassVar[str] = AZURE_ADVISOR_SOURCE_ID
    display_name: ClassVar[str] = "Azure Advisor (AKS)"
    tier: ClassVar[SourceTier] = SourceTier.CLOUD
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.NODE_POOL, Capability.HYGIENE})
    settings_model: ClassVar[type[SourceSettings]] = AzureAdvisorSettings
    #: Advisor refreshes about daily: at most one collection per hour.
    limits: ClassVar[SourceLimits] = SourceLimits(
        probe_timeout_seconds=15.0, collect_timeout_seconds=120.0, min_interval_seconds=3600.0
    )
    description: ClassVar[str] = (
        "Azure Advisor configuration advice about this AKS cluster (advisory only; Workload Identity, read only)."
    )

    def __init__(
        self,
        settings: SourceSettings,
        deps: SourceDeps,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        arm_url: str = ARM_URL,
        environ: Optional[Mapping[str, str]] = None,
    ):
        super().__init__(settings, deps)
        self.transport = transport
        self.arm_url = arm_url.rstrip("/")
        self.environ = environ if environ is not None else os.environ

    @property
    def options(self) -> AzureAdvisorSettings:
        settings = self.settings
        return settings if isinstance(settings, AzureAdvisorSettings) else AzureAdvisorSettings()

    def _client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.options.timeout_seconds, transport=self.transport, follow_redirects=False, **kwargs
        )

    def _missing(self) -> List[str]:
        missing = [
            f"SOURCES_CONFIG.azure_advisor.{name}"
            for name in ("subscription_id", "cluster_name")
            if not getattr(self.options, name)
        ]
        missing += [
            name
            for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE")
            if not self.environ.get(name)
        ]
        return missing

    async def discover(self) -> SourceHealth:
        missing = self._missing()
        if missing:
            return SourceHealth.unavailable(
                f"Azure Advisor needs {', '.join(missing)} (Azure Workload Identity on AKS)"
            )
        return SourceHealth.available(version=API_VERSION)

    async def _token(self) -> str:
        """An Azure Resource Manager token from the Workload Identity federated token."""
        env = self.environ
        with open(env["AZURE_FEDERATED_TOKEN_FILE"], encoding="utf-8") as handle:
            assertion = handle.read().strip()
        authority = (env.get("AZURE_AUTHORITY_HOST") or AUTHORITY_URL).rstrip("/") + "/"
        async with self._client() as client:
            response = await client.post(
                f"{authority}{env['AZURE_TENANT_ID']}/oauth2/v2.0/token",
                data={
                    "client_id": env["AZURE_CLIENT_ID"],
                    "scope": f"{ARM_URL}/.default",
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                    "grant_type": "client_credentials",
                },
            )
        if response.status_code != 200:
            raise RuntimeError(f"Azure AD refused the federated token (HTTP {response.status_code})")
        token = _mapping(response.json()).get("access_token")
        if not token:
            raise RuntimeError("Azure AD answered without an access token")
        return str(token)

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        options = self.options
        missing = self._missing()
        if missing:
            raise RuntimeError(f"missing {', '.join(missing)}")
        token = await self._token()
        categories = " or ".join(f"Category eq '{name}'" for name in options.categories)
        url: Optional[str] = (
            f"{self.arm_url}/subscriptions/{options.subscription_id}/providers/Microsoft.Advisor/recommendations"
        )
        first: Dict[str, str] = {"api-version": API_VERSION}
        if categories:
            first["$filter"] = categories
        params: Optional[Dict[str, str]] = first
        items: List[Mapping[str, Any]] = []
        foreign_link = False
        arm_origin = _origin(self.arm_url)
        async with self._client(headers={"Authorization": f"Bearer {token}"}) as client:
            for _ in range(MAX_PAGES):
                if url is None:
                    break
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = _mapping(response.json())
                items.extend(item for item in payload.get("value") or [] if isinstance(item, Mapping))
                next_link = payload.get("nextLink")
                url, params = (str(next_link), None) if next_link else (None, None)
                if url is not None and _origin(url) != arm_origin:
                    # The ARM token is only ever sent to Azure Resource Manager:
                    # stop paging (the batch stays incomplete) instead of following it.
                    foreign_link = True
                    break
        signals, counts = self.advice_signals(items, ctx)
        diagnostics: List[Diagnostic] = []
        if foreign_link:
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="foreign_next_link",
                    message="Advisor paged to another host than Azure Resource Manager; paging stopped",
                    count=1,
                )
            )
        diagnostics += [
            Diagnostic(
                level="info",
                code=code,
                message={
                    "other_resource": "recommendations about other resources than AKS clusters were ignored",
                    "other_cluster": "recommendations about other AKS clusters were ignored",
                    "duplicate": "duplicate recommendations were merged",
                }[code],
                count=amount,
            )
            for code, amount in sorted(counts.items())
        ]
        return SourceBatch(signals=signals, complete=url is None, diagnostics=diagnostics)

    def advice_signals(
        self, items: List[Mapping[str, Any]], ctx: CollectContext
    ) -> Tuple[List[SourceSignal], Dict[str, int]]:
        options = self.options
        cluster = (options.cluster_name or "").lower()
        group = (options.resource_group or "").lower()
        counts: Dict[str, int] = {}
        signals: Dict[str, SourceSignal] = {}
        now = self.deps.clock()
        for item in items:
            props = _mapping(item.get("properties"))
            if str(props.get("impactedField") or "").lower() != AKS_IMPACTED_FIELD:
                counts["other_resource"] = counts.get("other_resource", 0) + 1
                continue
            resource_id = str(_mapping(props.get("resourceMetadata")).get("resourceId") or item.get("id") or "")
            item_group = (_resource_group(resource_id) or "").lower()
            if str(props.get("impactedValue") or "").lower() != cluster or (group and item_group != group):
                counts["other_cluster"] = counts.get("other_cluster", 0) + 1
                continue
            short = _mapping(props.get("shortDescription"))
            problem = str(short.get("problem") or "").strip()
            solution = str(short.get("solution") or "").strip()
            text = solution or problem or "Review this Azure Advisor recommendation."
            category = str(props.get("category") or "")
            impact = str(props.get("impact") or "")
            extended = _mapping(props.get("extendedProperties"))
            savings = _number(extended.get("savingsAmount"))
            annual = _number(extended.get("annualSavingsAmount"))
            estimate: Optional[Dict[str, Any]] = None
            if savings is not None or annual is not None:
                estimate = {"currency": str(extended.get("savingsCurrency") or "USD")}
                if savings is not None:
                    estimate["monthly_savings"] = savings
                if annual is not None:
                    estimate["annual_savings"] = annual
            type_id = str(props.get("recommendationTypeId") or "")
            signal = SourceSignal(
                source_id=AZURE_ADVISOR_SOURCE_ID,
                source_version=API_VERSION,
                source_ref=str(item.get("name") or "") or None,
                target=TargetRef.for_kind(ctx.cluster_uid, "Cluster", ctx.cluster_uid),
                capability=capability_for(f"{problem} {solution}"),
                kind=f"AZURE_ADVISOR_{category.upper() or 'ADVICE'}",
                proposal=AdvisoryProposal(text=text),
                algorithm=AlgorithmInfo(
                    name="azure.advisor", params={"recommendation_type_id": type_id} if type_id else {}
                ),
                evidence=SignalEvidence(
                    summary=f"Azure Advisor ({category}, {impact} impact): {problem or text}",
                    details=solution,
                    data={"category": category, "impact": impact, "risk_class": "high", "resource_id": resource_id},
                ),
                source_estimate=estimate,
                provenance=SignalProvenance(origin=SignalOrigin.ADAPTER),
                raw=trim_raw({"id": item.get("id"), "name": item.get("name"), "properties": dict(props)}),
                fetched_at=now,
            )
            if signal.signal_id in signals:
                counts["duplicate"] = counts.get("duplicate", 0) + 1
                continue
            signals[signal.signal_id] = signal
        return list(signals.values()), counts
