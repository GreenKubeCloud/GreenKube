"""Azure Advisor (AKS) connector against a documented-shape fixture (roadmap G9)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs

import httpx

from greenkube.core.config import Config
from greenkube.signals.connectors.azure_advisor import (
    AzureAdvisorSettings,
    AzureAdvisorSource,
    capability_for,
)
from greenkube.signals.model import AdvisoryProposal, Capability
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import SourceDeps, SourceHealthStatus
from tests.sources.contract import CLUSTER, SourceContract, collect_context  # pyrefly: ignore[missing-import]

FIXTURE = Path(__file__).parent / "fixtures" / "azure_advisor.json"
ARM = "https://arm.test"
AUTHORITY = "https://login.test/"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"


class FakeAzure:
    def __init__(self, refuse_token: bool = False):
        self.pages = json.loads(FIXTURE.read_text())["pages"]
        self.refuse_token = refuse_token
        self.requests: List[httpx.Request] = []
        self.token_forms: List[Dict[str, List[str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(AUTHORITY):
            self.token_forms.append(parse_qs(request.content.decode()))
            if self.refuse_token:
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "arm-token", "token_type": "Bearer"})
        assert request.headers.get("Authorization") == "Bearer arm-token"
        if url == f"{ARM}/PAGE2":
            return httpx.Response(200, json=self.pages[1])
        if request.url.path == f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Advisor/recommendations":
            page = dict(self.pages[0])
            page["nextLink"] = f"{ARM}/PAGE2"
            return httpx.Response(200, json=page)
        return httpx.Response(404)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _environ(tmp_path: Path) -> Dict[str, str]:
    token = tmp_path / "azure-identity-token"
    token.write_text("federated.jwt.token\n")
    return {
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_FEDERATED_TOKEN_FILE": str(token),
        "AZURE_AUTHORITY_HOST": AUTHORITY,
    }


def _source(tmp_path: Path, server: Optional[FakeAzure] = None, environ=None, **settings) -> AzureAdvisorSource:
    settings.setdefault("subscription_id", SUBSCRIPTION)
    settings.setdefault("cluster_name", "aks-prod")
    return AzureAdvisorSource(
        AzureAdvisorSettings(**settings),
        SourceDeps(config=Config(), cluster_uid=CLUSTER, clock=_now),
        transport=httpx.MockTransport(server or FakeAzure()),
        arm_url=ARM,
        environ=_environ(tmp_path) if environ is None else environ,
    )


async def _run(source: AzureAdvisorSource):
    return await SourceRunner().run(source, collect_context(None))


async def test_aks_advice_becomes_cluster_advisories(tmp_path):
    server = FakeAzure()
    report = await _run(_source(tmp_path, server))

    assert report.run.status == SourceRunStatus.SUCCEEDED
    by_kind = {s.kind: s for s in report.signals}
    assert set(by_kind) == {"AZURE_ADVISOR_COST", "AZURE_ADVISOR_OPERATIONALEXCELLENCE"}
    autoscaler = by_kind["AZURE_ADVISOR_COST"]
    assert isinstance(autoscaler.proposal, AdvisoryProposal)
    assert autoscaler.target.kind == "Cluster" and autoscaler.target.name == CLUSTER
    assert autoscaler.capability == Capability.NODE_POOL
    assert autoscaler.source_estimate == {"currency": "EUR", "monthly_savings": 84.5, "annual_savings": 1014.0}
    assert autoscaler.evidence.data["risk_class"] == "high"
    assert by_kind["AZURE_ADVISOR_OPERATIONALEXCELLENCE"].capability == Capability.HYGIENE
    codes = {d.code: d.count for d in report.run.diagnostics}
    assert codes == {"other_cluster": 1, "other_resource": 1}
    # Workload Identity: the federated token is exchanged for an ARM token.
    form = server.token_forms[0]
    assert form["client_assertion"] == ["federated.jwt.token"]
    assert form["scope"] == ["https://management.azure.com/.default"]
    assert form["client_id"] == ["client-id"]
    assert server.requests[1].url.params["api-version"] == "2023-01-01"
    assert "Category eq 'Cost'" in server.requests[1].url.params["$filter"]


async def test_resource_group_narrows_the_match(tmp_path):
    report = await _run(_source(tmp_path, resource_group="rg-other"))

    assert report.signals == []
    assert {d.code: d.count for d in report.run.diagnostics}["other_cluster"] == 3


async def test_unavailable_without_workload_identity_or_cluster_name(tmp_path):
    no_identity = await _run(_source(tmp_path, environ={}))
    no_cluster = await _run(_source(tmp_path, cluster_name=None))

    for report in (no_identity, no_cluster):
        assert report.run.health_status == SourceHealthStatus.UNAVAILABLE
        assert report.signals == []
    assert "AZURE_FEDERATED_TOKEN_FILE" in (no_identity.run.health_reason or "")


async def test_refused_token_fails_the_run_without_leaking(tmp_path):
    report = await _run(_source(tmp_path, FakeAzure(refuse_token=True)))

    assert report.run.status == SourceRunStatus.FAILED
    assert "federated.jwt.token" not in (report.run.error or "")


def test_capability_keywords():
    assert capability_for("Enable the cluster autoscaler") == Capability.NODE_POOL
    assert capability_for("Use Spot node pools for batch work") == Capability.NODE_POOL
    assert capability_for("Enable the cost analysis add-on") == Capability.HYGIENE


def test_fixture_is_labelled_synthetic():
    assert json.loads(FIXTURE.read_text())["_provenance"]["kind"] == "synthetic"


class TestAzureAdvisorContract(SourceContract):
    def make_source(self):
        import tempfile

        return _source(Path(tempfile.mkdtemp()))

    def make_context(self):
        return collect_context(None)
