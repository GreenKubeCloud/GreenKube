"""Kubecost connector against 2.x and 3.x fixtures (roadmap G3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import pytest

from greenkube.core.config import Config
from greenkube.signals.connectors.kubecost import KubecostSettings, KubecostSource, probe_version, resolve_owner
from greenkube.signals.model import (
    AdvisoryProposal,
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    TargetRef,
)
from greenkube.signals.registry import SourceRegistry
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import CollectContext, SourceDeps, SourceHealthStatus, WorkloadInventory
from tests.sources.contract import CLUSTER, SourceContract, collect_context  # pyrefly: ignore[missing-import]

FIXTURES = Path(__file__).parent / "fixtures" / "kubecost"
URL = "http://kubecost.test:9090"
MIB = 1024 * 1024

ROUTES = {
    "/model/clusterInfo": "clusterInfo",
    "/model/savings/requestSizingV2": "requestSizingV2",
    "/model/savings/abandonedWorkloads": "abandonedWorkloads",
    "/model/savings/clusterSizingETL": "clusterSizingETL",
    "/model/savings/nodeGroupSizing": "nodeGroupSizing",
}


class FakeKubecost:
    """Serves a fixture; missing answers are 404, ``fail`` paths answer 500."""

    def __init__(self, version: str, fail: Optional[List[str]] = None):
        self.answers = json.loads((FIXTURES / f"{version}.json").read_text())
        self.fail = set(fail or [])
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.fail:
            return httpx.Response(500, json={"message": "boom"})
        key = ROUTES.get(path)
        if key is None or key not in self.answers:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=self.answers[key])

    def params(self, path: str) -> Dict[str, str]:
        return next(dict(r.url.params) for r in self.requests if r.url.path == path)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _source(server: FakeKubecost, **settings) -> KubecostSource:
    settings.setdefault("url", URL)
    return KubecostSource(
        KubecostSettings(**settings),
        SourceDeps(config=Config(), cluster_uid=CLUSTER, clock=_now),
        transport=httpx.MockTransport(server),
    )


async def _run(source: KubecostSource):
    return await SourceRunner().run(source, collect_context(None))


class TestKubecost2:
    async def test_probe_reports_the_major_version(self):
        report = await _run(_source(FakeKubecost("v2")))
        assert report.run.status == SourceRunStatus.SUCCEEDED
        assert report.run.health_status == SourceHealthStatus.AVAILABLE

    async def test_request_sizing_becomes_container_proposals(self):
        server = FakeKubecost("v2")
        report = await _run(_source(server, algorithm_cpu="quantile", q_cpu=0.95, filter='namespace:"shop"'))

        sizing = {(s.target.name, s.kind): s for s in report.signals if s.capability == Capability.CONTAINER_REQUESTS}
        assert set(sizing) == {
            ("api", "RIGHTSIZING_CPU"),
            ("api", "RIGHTSIZING_MEMORY"),
            ("postgres", "RIGHTSIZING_MEMORY"),
        }
        cpu = sizing[("api", "RIGHTSIZING_CPU")]
        assert isinstance(cpu.proposal, ContainerResourcesProposal)
        assert cpu.proposal.current.cpu_request_m == 1000 and cpu.proposal.proposed.cpu_request_m == 250
        assert cpu.target.key() == f"{CLUSTER}/apps.Deployment/shop/api/web"
        assert cpu.source_estimate == {"monthly_savings": 18.25, "currency": "USD"}
        assert cpu.algorithm.params["algorithmCPU"] == "quantile" and cpu.algorithm.params["qCPU"] == 0.95
        increase = sizing[("postgres", "RIGHTSIZING_MEMORY")]
        assert isinstance(increase.proposal, ContainerResourcesProposal)
        assert increase.proposal.direction == "increase"
        assert increase.proposal.proposed.memory_request_bytes == 768 * MIB
        params = server.params("/model/savings/requestSizingV2")
        assert params["window"] == "7d" and params["algorithmCPU"] == "quantile"
        assert params["targetCPUUtilization"] == "0.7" and params["filter"] == 'namespace:"shop"'
        assert "qRAM" not in params
        codes = {d.code for d in report.run.diagnostics}
        assert {"no_controller", "unchanged"} <= codes

    async def test_abandoned_workloads_are_deletions_for_review(self):
        report = await _run(_source(FakeKubecost("v2")))

        [cleanup] = [s for s in report.signals if s.capability == Capability.CLEANUP]
        assert isinstance(cleanup.proposal, DeletionProposal)
        assert cleanup.kind == "ABANDONED_WORKLOAD"
        assert cleanup.target.kind == "Deployment" and cleanup.target.name == "legacy-report"
        assert cleanup.evidence.data["deletion_requires_review"] is True
        assert cleanup.evidence.data["basis"] == "network_traffic"
        assert cleanup.evidence.data["ingress_bytes_per_second"] == 0.9
        assert cleanup.source_estimate == {"monthly_savings": 25.0, "currency": "USD"}
        assert "appears abandoned" in cleanup.evidence.summary

    async def test_cluster_sizing_is_advisory_only(self):
        report = await _run(_source(FakeKubecost("v2")))

        [node] = [s for s in report.signals if s.capability == Capability.NODE_POOL]
        assert isinstance(node.proposal, AdvisoryProposal)
        assert node.target.kind == "Cluster" and node.evidence.data["risk_class"] == "high"
        assert "310.50" in node.evidence.summary and "310" not in node.proposal.text

    async def test_disabled_capabilities_are_not_queried(self):
        server = FakeKubecost("v2")
        await _run(_source(server, abandoned_workloads=False, node_group_sizing=False))
        assert {r.url.path for r in server.requests} == {"/model/clusterInfo", "/model/savings/requestSizingV2"}


class TestKubecost3:
    async def test_uses_the_3x_path_map_and_wrapped_answers(self):
        server = FakeKubecost("v3")
        report = await _run(_source(server, cluster_id="prod-eu"))

        assert report.run.status == SourceRunStatus.SUCCEEDED
        paths = {r.url.path for r in server.requests}
        # The 3.x API directory documents the Cluster Right-Sizing API; there is no node-group sizing API.
        assert "/model/savings/clusterSizingETL" in paths and "/model/savings/nodeGroupSizing" not in paths
        cpu = [s for s in report.signals if s.kind == "RIGHTSIZING_CPU"]
        assert [s.source_version for s in cpu] == ["3.x"]
        assert isinstance(cpu[0].proposal, ContainerResourcesProposal) and cpu[0].proposal.proposed.cpu_request_m == 300

    async def test_documented_cluster_sizing_answer_is_read_for_this_cluster_only(self):
        report = await _run(_source(FakeKubecost("v3"), cluster_id="prod-eu"))

        [node] = [s for s in report.signals if s.capability == Capability.NODE_POOL]
        assert isinstance(node.proposal, AdvisoryProposal) and node.source_version == "3.x"
        assert node.evidence.data["monthly_savings_by_strategy"] == {"single": 120.0, "multi": 240.6}
        assert node.source_estimate == {"monthly_savings": 240.6, "currency": "USD"}
        assert node.raw["parameters"]["clusterId"] == "prod-eu"

    async def test_cluster_sizing_of_a_cluster_without_savings_advises_nothing(self):
        report = await _run(_source(FakeKubecost("v3"), cluster_id="prod-us"))

        assert not [s for s in report.signals if s.capability == Capability.NODE_POOL]
        assert "node_group_sizing_no_savings" in {d.code for d in report.run.diagnostics}

    async def test_cluster_sizing_of_an_unknown_cluster_id_is_not_guessed(self):
        server = FakeKubecost("v3")
        server.answers["requestSizingV2"] = {"Recommendations": []}
        report = await _run(_source(server, cluster_id="staging"))

        assert not [s for s in report.signals if s.capability == Capability.NODE_POOL]
        assert "node_group_sizing_other_cluster" in {d.code for d in report.run.diagnostics}

    def test_fixtures_are_labelled_synthetic(self):
        for version in ("v2", "v3"):
            provenance = json.loads((FIXTURES / f"{version}.json").read_text())["_provenance"]
            assert provenance["kind"] == "synthetic" and "Not recorded from a live" in provenance["basis"]

    async def test_several_clusters_without_cluster_id_are_not_guessed(self):
        report = await _run(_source(FakeKubecost("v3")))

        assert not [s for s in report.signals if s.capability == Capability.CONTAINER_REQUESTS]
        assert "multiple_clusters" in {d.code for d in report.run.diagnostics}

    async def test_a_missing_endpoint_is_a_diagnostic_not_a_failure(self):
        server = FakeKubecost("v3")
        del server.answers["clusterSizingETL"]
        report = await _run(_source(server, cluster_id="prod-eu"))

        assert report.run.status == SourceRunStatus.SUCCEEDED
        assert "path_not_found" in {d.code for d in report.run.diagnostics}

    async def test_path_overrides_win(self):
        server = FakeKubecost("v3")
        server.answers["nodeGroupSizing"] = server.answers.pop("clusterSizingETL")
        report = await _run(
            _source(server, cluster_id="prod-eu", paths={"node_group_sizing": "/model/savings/nodeGroupSizing"})
        )
        assert any(s.capability == Capability.NODE_POOL for s in report.signals)


def _inventory_context(*targets: TargetRef) -> CollectContext:
    context = collect_context(None)
    context.inventory = WorkloadInventory(targets=list(targets))
    return context


class TestAttribution:
    """Owners are resolved to their controller; cluster-level advice is never guessed."""

    async def test_abandoned_replicaset_owner_resolves_to_its_deployment(self):
        server = FakeKubecost("v2")
        for entry in server.answers["abandonedWorkloads"]:
            entry["owners"] = [{"name": "legacy-report-5c7d9f8b4", "kind": "replicaset"}]
        deployment = TargetRef.for_kind(CLUSTER, "Deployment", "legacy-report", namespace="batch", container="main")

        report = await SourceRunner().run(_source(server), _inventory_context(deployment))

        [cleanup] = [s for s in report.signals if s.capability == Capability.CLEANUP]
        assert (cleanup.target.kind, cleanup.target.name) == ("Deployment", "legacy-report")
        assert cleanup.evidence.data["pods"] == ["legacy-report-5c7d9-abcde", "legacy-report-5c7d9-fghij"]

    def test_resolve_owner_keeps_what_the_inventory_cannot_resolve(self):
        inventory = WorkloadInventory(
            targets=[
                TargetRef.for_kind(CLUSTER, "Deployment", "api", namespace="shop", container="web"),
                TargetRef.for_kind(CLUSTER, "Deployment", "api-gateway", namespace="shop", container="web"),
                TargetRef.for_kind(CLUSTER, "CronJob", "nightly", namespace="batch", container="job"),
            ]
        )
        assert resolve_owner(inventory, "shop", "ReplicaSet", "api-gateway-7d9f8c6b5") == ("Deployment", "api-gateway")
        assert resolve_owner(inventory, "shop", "ReplicaSet", "api-7d9f8c6b5") == ("Deployment", "api")
        assert resolve_owner(inventory, "batch", "Job", "nightly-29012345") == ("CronJob", "nightly")
        assert resolve_owner(inventory, "other", "ReplicaSet", "api-7d9f8c6b5") == ("ReplicaSet", "api-7d9f8c6b5")
        assert resolve_owner(inventory, "shop", "ReplicaSet", "api-Not_A_Hash") == ("ReplicaSet", "api-Not_A_Hash")
        assert resolve_owner(inventory, "shop", "Deployment", "api") == ("Deployment", "api")

    async def test_node_group_sizing_of_a_federated_kubecost_needs_cluster_id(self):
        report = await _run(_source(FakeKubecost("v3")))

        assert not [s for s in report.signals if s.capability == Capability.NODE_POOL]
        assert "node_group_sizing_other_cluster" in {d.code for d in report.run.diagnostics}

    async def test_node_group_sizing_naming_another_cluster_is_skipped(self):
        server = FakeKubecost("v3")
        # Legacy flat answer naming its cluster.
        server.answers["clusterSizingETL"] = {"code": 200, "data": {"clusterId": "staging", "monthlySavings": 50.0}}
        report = await _run(_source(server, cluster_id="prod-eu"))

        assert not [s for s in report.signals if s.capability == Capability.NODE_POOL]
        assert "node_group_sizing_other_cluster" in {d.code for d in report.run.diagnostics}

    @pytest.mark.parametrize("savings", [0, None])
    async def test_node_group_sizing_without_positive_savings_is_not_advice(self, savings):
        server = FakeKubecost("v2")
        server.answers["clusterSizingETL"]["data"]["totalMonthlySavings"] = savings
        report = await _run(_source(server))

        assert not [s for s in report.signals if s.capability == Capability.NODE_POOL]
        assert "node_group_sizing_no_savings" in {d.code for d in report.run.diagnostics}


class TestFailures:
    async def test_unreachable_kubecost_is_unavailable(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        source = KubecostSource(
            KubecostSettings(url=URL),
            SourceDeps(config=Config(), cluster_uid=CLUSTER),
            transport=httpx.MockTransport(refuse),
        )
        report = await _run(source)
        assert report.run.status == SourceRunStatus.UNAVAILABLE
        assert "not reachable" in (report.run.health_reason or "")

    async def test_failing_endpoint_makes_the_batch_incomplete(self):
        report = await _run(_source(FakeKubecost("v2", fail=["/model/savings/abandonedWorkloads"])))
        assert report.run.status == SourceRunStatus.INCOMPLETE
        assert any(s.capability == Capability.CONTAINER_REQUESTS for s in report.signals)

    async def test_without_url_outside_a_cluster_it_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        source = KubecostSource(KubecostSettings(), SourceDeps(config=Config(), cluster_uid=CLUSTER))
        report = await _run(source)
        assert report.run.status == SourceRunStatus.UNAVAILABLE
        assert "SOURCES_CONFIG.kubecost.url" in (report.run.health_reason or "")


@pytest.mark.parametrize(
    ("payload", "major"),
    [({"data": {"version": "2.4.1"}}, "2"), ({"version": "v3.0.2"}, "3"), ({"data": {}}, None), ([], None)],
)
def test_probe_version(payload, major):
    assert probe_version(payload) == major


def test_settings_validate_paths_and_window():
    with pytest.raises(ValueError):
        KubecostSettings(paths={"unknown": "/x"})
    with pytest.raises(ValueError):
        KubecostSettings(window="12h")
    registry = SourceRegistry()
    registry.register(KubecostSource)
    [configured] = registry.configure({"kubecost": {"url": URL, "cluster_id": "c1"}})
    assert configured.error is None


class TestKubecostContract(SourceContract):
    def make_source(self):
        return _source(FakeKubecost("v2"))

    def make_context(self):
        return collect_context(None)
