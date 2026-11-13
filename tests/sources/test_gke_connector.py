"""GKE recommender insights connector against a recorded-shape fixture (roadmap G7)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import httpx
import pytest

from greenkube.core.config import Config
from greenkube.signals.connectors.gke import (
    QUOTA_PROJECT_PERMISSION,
    REQUIRED_ROLES,
    GkeRecommenderSource,
    GkeSettings,
    container_proposals,
    parse_target,
)
from greenkube.signals.model import AdvisoryProposal, Capability, ContainerResourcesProposal
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import SourceDeps, SourceHealthStatus
from tests.sources.contract import CLUSTER, SourceContract, collect_context  # pyrefly: ignore[missing-import]

FIXTURE = Path(__file__).parent / "fixtures" / "gke_insights.json"
GKE_DOC = Path(__file__).resolve().parents[2] / "docs" / "sources" / "gke.md"
METADATA = "http://metadata.test"
API = "https://recommender.test"
MIB = 1024 * 1024


class FakeGoogle:
    """The metadata server and the Recommender API, served from the fixture."""

    def __init__(self, on_gke: bool = True):
        self.data = json.loads(FIXTURE.read_text())
        self.on_gke = on_gke
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(METADATA):
            if not self.on_gke or request.headers.get("Metadata-Flavor") != "Google":
                return httpx.Response(404)
            path = request.url.path.removeprefix("/computeMetadata/v1/")
            value = self.data["metadata"].get(path)
            if value is None:
                return httpx.Response(404, headers={"Metadata-Flavor": "Google"})
            return httpx.Response(200, text=value, headers={"Metadata-Flavor": "Google"})
        assert request.headers.get("Authorization") == "Bearer ya29.test-token"
        path = request.url.path
        if path == "/v1beta1/insightTypes":
            return httpx.Response(200, json=self.data["insightTypes"])
        prefix = "/v1/projects/acme-prod/locations/"
        if path.startswith(prefix) and path.endswith("/insightTypes/google.container.DiagnosisInsight/insights"):
            location = path[len(prefix) :].split("/", 1)[0]
            token = request.url.params.get("pageToken")
            key = f"{location}#{token}" if token else location
            answer = self.data["insights"].get(key)
            if answer is None:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, json=answer)
        return httpx.Response(404)

    def paths(self) -> List[str]:
        return [r.url.path for r in self.requests if str(r.url).startswith(API)]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _source(server: Optional[FakeGoogle] = None, **settings) -> GkeRecommenderSource:
    return GkeRecommenderSource(
        GkeSettings(**settings),
        SourceDeps(config=Config(), cluster_uid=CLUSTER, clock=_now),
        transport=httpx.MockTransport(server or FakeGoogle()),
        metadata_url=METADATA,
        api_url=API,
    )


async def _run(source: GkeRecommenderSource):
    return await SourceRunner().run(source, collect_context(None))


class TestGke:
    async def test_insight_types_are_discovered_at_runtime(self):
        server = FakeGoogle()
        report = await _run(_source(server))

        assert report.run.status == SourceRunStatus.SUCCEEDED
        paths = server.paths()
        assert paths[0] == "/v1beta1/insightTypes"
        # Only google.container.* types are read, in the zone and its region.
        assert all("google.compute" not in path for path in paths)
        assert any("/locations/europe-west1-b/" in path for path in paths)
        assert any("/locations/europe-west1/" in path for path in paths)

    async def test_overprovisioned_insight_becomes_container_proposals(self):
        report = await _run(_source())

        sizing = {s.kind: s for s in report.signals if isinstance(s.proposal, ContainerResourcesProposal)}
        assert set(sizing) == {"RIGHTSIZING_CPU", "RIGHTSIZING_MEMORY"}
        cpu = sizing["RIGHTSIZING_CPU"]
        assert isinstance(cpu.proposal, ContainerResourcesProposal)
        assert cpu.target.key() == f"{CLUSTER}/apps.Deployment/shop/api/web"
        assert cpu.proposal.current.cpu_request_m == 1000 and cpu.proposal.proposed.cpu_request_m == 260
        assert cpu.proposal.direction == "decrease"
        assert cpu.algorithm.name == "gke.recommender"
        assert cpu.algorithm.params["insight_subtype"] == "WORKLOAD_OVERPROVISIONED"
        assert cpu.algorithm.window is not None
        assert (cpu.algorithm.window.end - cpu.algorithm.window.start).days == 15
        assert cpu.source_version == "google.container.DiagnosisInsight"
        memory = sizing["RIGHTSIZING_MEMORY"]
        assert isinstance(memory.proposal, ContainerResourcesProposal)
        assert memory.proposal.proposed.memory_request_bytes == 512 * MIB

    async def test_insight_without_values_stays_advice(self):
        report = await _run(_source())

        [advice] = [s for s in report.signals if isinstance(s.proposal, AdvisoryProposal)]
        assert advice.kind == "WORKLOAD_UNDERPROVISIONED"
        assert advice.target.kind == "StatefulSet" and advice.target.name == "db"
        assert isinstance(advice.proposal, AdvisoryProposal)
        assert "increase its requests" in advice.proposal.text
        assert advice.capability == Capability.CONTAINER_REQUESTS

    async def test_other_clusters_inactive_and_other_subtypes_are_skipped_with_diagnostics(self):
        report = await _run(_source())

        codes = {d.code: d.count for d in report.run.diagnostics}
        assert codes["other_cluster"] == 1 and codes["inactive"] == 1 and codes["other_subtype"] == 1
        assert "gke_coverage" in codes
        assert not [s for s in report.signals if s.target.name == "worker"]

    async def test_pinned_insight_types_skip_discovery(self):
        server = FakeGoogle()
        await _run(_source(server, insight_types=["google.container.DiagnosisInsight"]))

        assert "/v1beta1/insightTypes" not in server.paths()

    async def test_unavailable_outside_gke(self):
        report = await _run(_source(FakeGoogle(on_gke=False)))

        assert report.run.status != SourceRunStatus.FAILED
        assert report.run.health_status == SourceHealthStatus.UNAVAILABLE
        assert report.signals == []

    async def test_settings_override_the_metadata(self):
        report = await _run(_source(cluster_name="staging-gke"))

        # The staging insight is now this cluster's; prod-gke insights are other clusters.
        assert [s.target.name for s in report.signals if isinstance(s.proposal, ContainerResourcesProposal)] == ["api"]
        assert {d.code: d.count for d in report.run.diagnostics}["other_cluster"] == 2

    async def test_same_named_cluster_at_another_location_is_skipped(self):
        # A regional prod-gke in europe-west1 is another cluster than the zonal
        # prod-gke in europe-west1-b, although the region's insights are read.
        server = FakeGoogle()
        [template] = [
            i for i in server.data["insights"]["europe-west1-b"]["insights"] if i["name"].endswith("over-api")
        ]
        regional = json.loads(json.dumps(template))
        regional["name"] = regional["name"].replace("europe-west1-b", "europe-west1").replace("over-api", "regional")
        regional["targetResources"] = [
            "//container.googleapis.com/projects/acme-prod/locations/europe-west1/clusters/prod-gke"
            "/k8s/namespaces/shop/apps/deployments/regional-only"
        ]
        server.data["insights"]["europe-west1"] = {"insights": [regional]}

        report = await _run(_source(server))

        assert not [s for s in report.signals if s.target.name == "regional-only"]
        assert {d.code: d.count for d in report.run.diagnostics}["other_cluster"] == 2

    async def test_targets_naming_the_project_number_are_this_project(self):
        server = FakeGoogle()
        for insight in server.data["insights"]["europe-west1-b"]["insights"]:
            insight["targetResources"] = [
                r.replace("/projects/acme-prod/", "/projects/123456/") for r in insight["targetResources"]
            ]

        report = await _run(_source(server))

        names = {s.target.name for s in report.signals if isinstance(s.proposal, ContainerResourcesProposal)}
        assert names == {"api"}
        # Another project's number is still another cluster.
        server = FakeGoogle()
        for insight in server.data["insights"]["europe-west1-b"]["insights"]:
            insight["targetResources"] = [
                r.replace("/projects/acme-prod/", "/projects/999999/") for r in insight["targetResources"]
            ]
        report = await _run(_source(server))
        assert not [s for s in report.signals if isinstance(s.proposal, ContainerResourcesProposal)]

    async def test_every_recommender_call_names_the_project_as_quota_project(self):
        server = FakeGoogle()
        await _run(_source(server))

        calls = [r for r in server.requests if str(r.url).startswith(API)]
        assert calls and calls[0].url.path == "/v1beta1/insightTypes"
        assert {r.headers.get("X-Goog-User-Project") for r in calls} == {"acme-prod"}

    def test_documented_permissions_cover_the_quota_project_header(self):
        # The header above needs serviceusage.services.use: the documented IAM
        # setup must grant it, or the first discovery call answers 403.
        doc = GKE_DOC.read_text()
        for role in REQUIRED_ROLES:
            assert role in doc
        assert "roles/serviceusage.serviceUsageConsumer" in REQUIRED_ROLES
        assert QUOTA_PROJECT_PERMISSION in doc

    def test_fixture_is_labelled_synthetic(self):
        assert json.loads(FIXTURE.read_text())["_provenance"]["kind"] == "synthetic"


@pytest.mark.parametrize(
    "resource,expected",
    [
        (
            "//container.googleapis.com/projects/p1-x/locations/us-central1/clusters/c/k8s/namespaces/ns/apps/deployments/d",
            ("Deployment", "ns", "d", "c"),
        ),
        (
            "//container.googleapis.com/projects/p1-x/zones/us-central1-a/clusters/c/k8s/namespaces/ns/batch/cronjobs/j",
            ("CronJob", "ns", "j", "c"),
        ),
        ("//container.googleapis.com/projects/p/locations/l/clusters/c/nodePools/default", None),
        ("//compute.googleapis.com/projects/p/zones/z/instances/i", None),
    ],
)
def test_parse_target(resource, expected):
    parsed = parse_target(resource)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (parsed["kind"], parsed["namespace"], parsed["name"], parsed["cluster"]) == expected


def test_container_content_is_read_defensively():
    assert container_proposals({"containers": [{"name": "a", "recommended": {"cpu": 0.5}}]}) == [
        ("a", "cpu", None, 500)
    ]
    assert container_proposals({"recommendation": {"containerRecommendations": [{"containerName": "b"}]}}) == []
    assert container_proposals({"containers": [{"recommended": {"cpu": "1"}}]}) == []
    assert container_proposals(None) == []


def test_zero_recommended_requests_are_dropped():
    content = {"containers": [{"name": "a", "current": {"cpu": "500m"}, "recommended": {"cpu": "0", "memory": 0}}]}
    assert container_proposals(content) == []


class TestGkeSourceContract(SourceContract):
    builtin = True

    def make_source(self):
        return _source()

    def make_context(self):
        return collect_context(None)
