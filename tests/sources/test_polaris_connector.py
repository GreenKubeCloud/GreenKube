"""Fairwinds Polaris hygiene findings (roadmap G4)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.scoring import default_delivery_mode
from greenkube.models.metrics import CombinedMetric, RecommendationType
from greenkube.signals.connectors.inbox import InboxBatch
from greenkube.signals.connectors.polaris import (
    FOLLOW_UP_ENGINE,
    PolarisReportError,
    PolarisSource,
    finding_kind,
    parse_polaris_report,
)
from greenkube.signals.connectors.replay import InboxSettings
from greenkube.signals.model import AdvisoryProposal, Capability, ContainerResourcesProposal
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import SourceDeps
from tests.sources.contract import CLUSTER, SourceContract, collect_context  # pyrefly: ignore[missing-import]
from tests.sources.fake_inbox import MemoryInbox  # pyrefly: ignore[missing-import]

FIXTURE = Path(__file__).parent / "fixtures" / "polaris_report.json"
MIB = 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _report() -> dict:
    return json.loads(FIXTURE.read_text())


def _parsed(cluster: str = CLUSTER):
    return parse_polaris_report(_report(), cluster_uid=cluster, received_at=_now())


def _source(config: Config | None = None, cluster: str = CLUSTER) -> PolarisSource:
    result = _parsed(cluster)
    received = _now()
    inbox = MemoryInbox()
    inbox.seed(InboxBatch("polaris-1", "polaris", received, True, len(result.signals)), result.signals)
    return PolarisSource(InboxSettings(), SourceDeps(config=config or Config(), cluster_uid=cluster, clock=_now), inbox)


def _usage_context(config: Config, *, cpu_request: int = 0, samples: int = 48) -> OptimizationContext:
    now = _now()
    metrics = [
        CombinedMetric(
            pod_name=f"api-7d9f8b6c5-{'x2k4p' if index % 2 else 'q8w2z'}",
            namespace="shop",
            owner_kind="Deployment",
            owner_name="api",
            container_name="web",
            cpu_request=cpu_request,
            memory_request=0,
            cpu_usage_millicores=100 + (index % 10) * 10,
            memory_usage_bytes=(200 + (index % 10) * 10) * MIB,
            total_cost=0.01,
            co2e_grams=1.0,
            timestamp=now - timedelta(minutes=5 * (index // 2 + 1)),
        )
        for index in range(samples)
    ]
    return OptimizationContext(config=config, metrics=metrics, container_metrics=metrics, analysis_window_seconds=86400)


class TestParser:
    def test_failed_checks_become_hygiene_advisories(self):
        result = _parsed()

        kinds = {(s.target.name, s.target.container, s.kind) for s in result.signals}
        assert kinds == {
            ("api", None, "DEPLOYMENT_MISSING_REPLICAS"),
            ("api", "web", "CPU_REQUESTS_MISSING"),
            ("api", "web", "MEMORY_REQUESTS_MISSING"),
            ("api", "web", "RUN_AS_ROOT_ALLOWED"),
            ("db", "postgres", "LIVENESS_PROBE_MISSING"),
        }
        assert all(s.capability == Capability.HYGIENE for s in result.signals)
        assert all(isinstance(s.proposal, AdvisoryProposal) for s in result.signals)
        root = next(s for s in result.signals if s.kind == "RUN_AS_ROOT_ALLOWED")
        assert root.confidence == 0.8 and root.evidence.data["category"] == "Security"
        assert root.target.key() == f"{CLUSTER}/apps.Deployment/shop/api/web"
        cpu = next(s for s in result.signals if s.kind == "CPU_REQUESTS_MISSING")
        assert cpu.evidence.data["follow_up"] == "greenkube.requests_from_usage"
        codes = {d.code: d.count for d in result.diagnostics}
        assert codes["passed"] == 3 and codes["ignored_severity"] == 1 and codes["invalid_entry"] == 1

    def test_signal_ids_are_stable_across_reports(self):
        assert [s.signal_id for s in _parsed().signals] == [s.signal_id for s in _parsed().signals]

    @pytest.mark.parametrize("document", [[], {"Score": 3}, {"Results": {}}])
    def test_other_documents_are_refused(self, document):
        with pytest.raises(PolarisReportError):
            parse_polaris_report(document, cluster_uid=CLUSTER, received_at=_now())

    def test_finding_kind(self):
        assert finding_kind("cpuRequestsMissing") == "CPU_REQUESTS_MISSING"
        assert finding_kind("hostIPCSet") == "HOST_IPCSET"
        assert finding_kind("custom-check.v2") == "CUSTOM_CHECK_V2"

    def test_fixture_is_labelled_synthetic(self):
        assert _report()["_provenance"]["kind"] == "synthetic"


class TestFollowUp:
    async def test_missing_requests_get_a_request_from_observed_usage(self):
        config = Config()
        report = await SourceRunner().run(_source(config), collect_context(_usage_context(config)))

        assert report.run.status == SourceRunStatus.SUCCEEDED
        follow_ups = {s.kind: s for s in report.signals if s.capability == Capability.CONTAINER_REQUESTS}
        assert set(follow_ups) == {"RIGHTSIZING_CPU", "RIGHTSIZING_MEMORY"}
        cpu = follow_ups["RIGHTSIZING_CPU"]
        assert isinstance(cpu.proposal, ContainerResourcesProposal)
        assert cpu.engine_family == FOLLOW_UP_ENGINE and cpu.source_id == "polaris"
        p95 = cpu.evidence.data["p95"]
        assert cpu.proposal.proposed.cpu_request_m == max(round(p95 * config.RIGHTSIZING_HEADROOM), 10)
        # No request observed: the current request is zero, so this is an increase.
        assert cpu.proposal.current.cpu_request_m == 0
        assert cpu.proposal.direction == "increase"
        assert cpu.algorithm.name == "greenkube.requests_from_usage"
        # The Polaris findings themselves are still there (hygiene advice).
        assert sum(s.capability == Capability.HYGIENE for s in report.signals) == 5

    async def test_no_follow_up_without_enough_usage(self):
        config = Config()
        report = await SourceRunner().run(_source(config), collect_context(_usage_context(config, samples=4)))

        assert not [s for s in report.signals if s.capability == Capability.CONTAINER_REQUESTS]
        assert {d.code: d.count for d in report.run.diagnostics}["follow_up_without_usage"] == 2

    async def test_no_follow_up_once_the_container_has_a_request(self):
        config = Config()
        context = _usage_context(config, cpu_request=250)
        report = await SourceRunner().run(_source(config), collect_context(context))

        assert "RIGHTSIZING_CPU" not in {s.kind for s in report.signals}

    async def test_follow_up_becomes_a_recommendation_through_consensus(self):
        config = Config().model_copy(update={"CLUSTER_NAME": CLUSTER})
        engine = OptimizationEngine(config=config, sources=[_source(config)])
        engine.cluster_uid = CLUSTER
        result = await engine.collect(_usage_context(config))

        recs: List = [
            rec
            for rec in result.recommendations
            if rec.owner_name == "api"
            and rec.container_name == "web"
            and rec.type == RecommendationType.RIGHTSIZING_CPU
        ]
        assert recs, [r.type for r in result.recommendations]
        assert str(getattr(recs[0].source, "value", recs[0].source)) == "polaris"
        # Setting a request from unset is an increase: reviewed, never delivered directly.
        assert recs[0].current_cpu_request_millicores == 0
        assert default_delivery_mode(recs[0]) == "manual"
        # Hygiene advisories have no v1 type: they stay signals (never pull requests).
        assert not [r for r in result.recommendations if str(r.type).endswith("ROOT_ALLOWED")]


class TestPolarisSourceContract(SourceContract):
    def make_source(self):
        return _source()

    def make_context(self):
        return collect_context(None)
