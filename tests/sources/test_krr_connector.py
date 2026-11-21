"""Robusta KRR connector: report parsing and the krr source (roadmap G2).

``fixtures/krr_report.json`` follows KRR 1.x's ``Result`` model (``scans[]`` of
``ResourceScan`` with ``object``, ``recommended`` and ``severity``; strategy
``simple``). It holds a decrease, an increase after an OOM kill, a resource
without enough data, an unchanged CPU request, a container without requests
and a field unknown to this parser.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.signals.connectors.inbox import InboxBatch
from greenkube.signals.connectors.krr import KrrReportError, KrrSource, parse_krr_report
from greenkube.signals.connectors.replay import InboxSettings
from greenkube.signals.model import ContainerResourcesProposal
from greenkube.signals.runtime import SourceRunStatus
from greenkube.signals.sdk import SourceDeps
from greenkube.storage.base_signal_repository import create_signal_repository
from tests.sources.contract import CLUSTER, SourceContract, collect_context  # pyrefly: ignore[missing-import]
from tests.sources.fake_inbox import MemoryInbox  # pyrefly: ignore[missing-import]

FIXTURE = Path(__file__).parent / "fixtures" / "krr_report.json"
MIB = 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _report() -> dict:
    return json.loads(FIXTURE.read_text())


def _parse(document=None, received_at=None):
    return parse_krr_report(document or _report(), cluster_uid=CLUSTER, received_at=received_at or _now())


def _by_key(result):
    return {(s.target.name, s.kind): s for s in result.signals}


def _resources(signal) -> ContainerResourcesProposal:
    assert isinstance(signal.proposal, ContainerResourcesProposal)
    return signal.proposal


class TestParse:
    def test_every_container_yields_signals(self):
        result = _parse()

        assert result.entries == 4
        assert {s.target.container for s in result.signals} == {"web", "postgres", "exporter", "report"}
        assert set(_by_key(result)) == {
            ("api", "RIGHTSIZING_CPU"),
            ("api", "RIGHTSIZING_MEMORY"),
            ("postgres", "RIGHTSIZING_MEMORY"),
            ("node-exporter", "RIGHTSIZING_MEMORY"),
            ("nightly-report", "RIGHTSIZING_CPU"),
            ("nightly-report", "RIGHTSIZING_MEMORY"),
        }
        assert all(s.source_id == "krr" for s in result.signals)

    def test_targets_values_and_limits(self):
        signals = _by_key(_parse())
        cpu = signals[("api", "RIGHTSIZING_CPU")]
        assert cpu.target.key() == f"{CLUSTER}/apps.Deployment/shop/api/web"
        assert cpu.source_ref == "shop/Deployment/api"
        proposal = _resources(cpu)
        assert proposal.current.cpu_request_m == 1000 and proposal.proposed.cpu_request_m == 250
        assert proposal.proposed.cpu_limit_m is None and proposal.direction == "decrease"
        assert cpu.evidence.data["limit_advice"] == "KRR recommends no cpu limit"
        memory = _resources(signals[("api", "RIGHTSIZING_MEMORY")])
        assert memory.proposed.memory_request_bytes == 512 * MIB
        assert memory.proposed.memory_limit_bytes == 512 * MIB

    def test_increases_are_included(self):
        signal = _by_key(_parse())[("postgres", "RIGHTSIZING_MEMORY")]
        proposal = _resources(signal)
        assert proposal.direction == "increase"
        assert proposal.proposed.memory_request_bytes == 768 * MIB
        assert signal.evidence.details == "OOMKill detected"
        assert signal.target.api_version == "apps/v1" and signal.target.kind == "StatefulSet"

    def test_strategy_settings_become_algorithm_params(self):
        received = _now()
        signal = _parse(received_at=received).signals[0]
        assert signal.algorithm.name == "krr.simple"
        assert signal.algorithm.params["cpu_percentile"] == 95.0
        assert signal.algorithm.params["memory_buffer_percentage"] == 15.0
        assert signal.algorithm.window is not None
        assert signal.algorithm.window.end - signal.algorithm.window.start == timedelta(hours=336)
        assert signal.observed_window == signal.algorithm.window

    def test_severity_is_a_confidence_hint(self):
        signals = _by_key(_parse())
        assert signals[("api", "RIGHTSIZING_CPU")].confidence == 0.8
        assert signals[("api", "RIGHTSIZING_CPU")].evidence.data["severity"] == "CRITICAL"
        assert signals[("api", "RIGHTSIZING_MEMORY")].confidence == 0.7
        assert signals[("node-exporter", "RIGHTSIZING_MEMORY")].confidence == 0.6

    def test_skips_are_diagnostics(self):
        codes = {d.code: d.count for d in _parse().diagnostics}
        assert codes == {"not_enough_data": 1, "unchanged": 1}

    def test_unknown_fields_and_broken_entries_fail_soft(self):
        report = _report()
        report["future_top_level"] = {"x": 1}
        report["scans"].append({"object": {"kind": "Deployment"}})
        report["scans"].append("garbage")
        broken = copy.deepcopy(report["scans"][0])
        broken["recommended"]["requests"]["cpu"] = {"value": "NaN?", "severity": "WARNING"}
        broken["object"]["name"] = "api-2"
        report["scans"].append(broken)

        result = _parse(report)

        codes = {d.code: d.count for d in result.diagnostics}
        assert codes["invalid_entry"] == 2
        assert codes["not_enough_data"] == 2
        assert ("api-2", "RIGHTSIZING_MEMORY") in _by_key(result)

    def test_raw_drops_pod_lists_and_labels(self):
        raw = _by_key(_parse())[("api", "RIGHTSIZING_CPU")].raw
        assert raw["object"]["pod_count"] == 2
        assert "pods" not in raw["object"] and "labels" not in raw["object"]
        assert raw["object"]["cluster"] == "prod-eu"

    @pytest.mark.parametrize("document", [[], {"scan": []}, {"scans": {}}, "text"])
    def test_documents_that_are_not_reports_raise(self, document):
        with pytest.raises(KrrReportError):
            parse_krr_report(document, cluster_uid=CLUSTER, received_at=_now())

    def test_signal_ids_are_stable_across_reports(self):
        first = {s.signal_id for s in _parse(received_at=_now() - timedelta(days=1)).signals}
        second = {s.signal_id for s in _parse().signals}
        assert first == second


def _two_cluster_report() -> dict:
    """The fixture plus a staging copy of shop/api (``krr simple --all-clusters``)."""
    report = _report()
    staging = copy.deepcopy(report["scans"][0])
    staging["object"]["cluster"] = "staging"
    staging["recommended"]["requests"]["cpu"] = {"value": 4.0, "severity": "CRITICAL"}
    report["scans"].append(staging)
    return report


class TestClusters:
    """KRR's cluster is a kubeconfig context: several clusters are never mixed into this one."""

    def test_a_report_of_several_clusters_is_refused(self):
        with pytest.raises(KrrReportError) as raised:
            _parse(_two_cluster_report())
        assert raised.value.type == "multiple_clusters"
        assert raised.value.pointer == "/scans"
        assert "prod-eu" in str(raised.value) and "staging" in str(raised.value)

    def test_cluster_context_keeps_one_cluster_and_reports_the_rest(self):
        result = parse_krr_report(
            _two_cluster_report(), cluster_uid=CLUSTER, received_at=_now(), cluster_context="prod-eu"
        )

        cpu = [s for s in result.signals if (s.target.name, s.kind) == ("api", "RIGHTSIZING_CPU")]
        assert len(cpu) == 1 and _resources(cpu[0]).proposed.cpu_request_m == 250
        assert all(s.raw["object"]["cluster"] == "prod-eu" for s in result.signals)
        assert result.entries == 4
        other = next(d for d in result.diagnostics if d.code == "other_cluster")
        assert other.level == "warning" and other.count == 1

    def test_cluster_context_absent_from_the_report_is_refused(self):
        with pytest.raises(KrrReportError) as raised:
            parse_krr_report(_two_cluster_report(), cluster_uid=CLUSTER, received_at=_now(), cluster_context="dev")
        assert raised.value.type == "cluster_not_in_report"

    def test_unnamed_in_cluster_report_needs_no_context(self):
        report = _report()
        for entry in report["scans"]:
            entry["object"]["cluster"] = None
        assert len(_parse(report).signals) == 6
        selected = parse_krr_report(report, cluster_uid=CLUSTER, received_at=_now(), cluster_context="prod-eu")
        assert len(selected.signals) == 6
        assert "other_cluster" not in {d.code for d in selected.diagnostics}


def _stored_inbox(received_at=None) -> MemoryInbox:
    inbox = MemoryInbox()
    received = received_at or _now()
    result = _parse(received_at=received)
    inbox.seed(InboxBatch("r1", "krr", received, True, len(result.signals)), result.signals)
    return inbox


def _source(inbox: MemoryInbox) -> KrrSource:
    return KrrSource(InboxSettings(), SourceDeps(config=Config(), cluster_uid=CLUSTER), inbox=inbox)


class TestKrrSourceContract(SourceContract):
    def make_source(self):
        return _source(_stored_inbox())

    def make_context(self):
        return collect_context(None)


async def test_scan_persists_krr_source_runs_and_signals(tmp_path, monkeypatch):
    """End to end: a stored report is replayed by a scan, recorded in source_runs/source_signals and consolidated."""
    from greenkube.core.config import config

    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "krr.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        repository = create_signal_repository(manager, "sqlite")
        engine = OptimizationEngine(config=Config(), sources=[_source(_stored_inbox())], signal_repository=repository)
        engine.cluster_uid = CLUSTER
        result = await engine.collect(OptimizationContext(config=Config()))
        await engine.record_source_runs(result.source_reports, None)

        # KRR alone, with no native measurement of the targets, never creates a recommendation (A.5):
        # its advice stays a signal, recorded for the Sources page.
        assert result.recommendations == []
        runs = await repository.list_runs("krr")
        assert [run.status for run in runs] == [SourceRunStatus.SUCCEEDED]
        assert runs[0].signal_count == 6
        stored = await repository.list_signals("krr")
        assert len(stored) == 6
        assert {item.signal.target.container for item in stored} == {"web", "postgres", "exporter", "report"}
    finally:
        await manager.close()
