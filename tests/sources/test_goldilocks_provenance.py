"""Goldilocks provenance inside the VPA connector (roadmap G1).

The fixture holds one Goldilocks VPA and one user VPA on the same container
(they disagree on CPU and agree on memory) and one Goldilocks VPA in ``Auto``
mode, which is skipped.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from greenkube.collectors.vpa_collector import VPACollector, VPARecommendation
from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.providers.vpa import (
    GOLDILOCKS_SOURCE_ID,
    VPA_ENGINE_FAMILY,
    VpaSignalSource,
    VpaSource,
    is_goldilocks_vpa,
)
from greenkube.models.metrics import CombinedMetric, RecommendationType
from greenkube.signals.model import ContainerResourcesProposal
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import Source, SourceBatch, SourceSettings, validate_source_class
from tests.sources.contract import SourceContract, collect_context, deps  # pyrefly: ignore[missing-import]

FIXTURE = Path(__file__).parent / "fixtures" / "goldilocks_vpas.json"
MIB = 1024 * 1024


class FixtureCollector:
    """The real VPA collector reading the recorded VPA list."""

    def __init__(self, payload: Optional[dict] = None):
        self.payload = payload if payload is not None else json.loads(FIXTURE.read_text())
        self.skipped_by_mode: dict = {}

    async def collect(self) -> List[VPARecommendation]:
        api = AsyncMock()
        api.list_cluster_custom_object = AsyncMock(return_value=self.payload)
        inner = VPACollector()
        with patch("greenkube.collectors.vpa_collector.get_custom_objects_api", AsyncMock(return_value=api)):
            recs = await inner.collect()
        self.skipped_by_mode = inner.skipped_by_mode
        return recs


def _context(cpu_request: int = 1000, memory_request: int = 1024 * MIB) -> OptimizationContext:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    metric = CombinedMetric(
        pod_name="api-7d9f8b6c5-x2k4p",
        namespace="shop",
        owner_kind="Deployment",
        owner_name="api",
        container_name="web",
        cpu_request=cpu_request,
        memory_request=memory_request,
        total_cost=1.0,
        co2e_grams=10.0,
        timestamp=now - timedelta(hours=1),
    )
    return OptimizationContext(config=Config(), metrics=[metric], analysis_window_seconds=7 * 86400)


def _source(collector=None, namespaces: Optional[List[str]] = None) -> VpaSignalSource:
    connector = VpaSource(Config(), collector=collector or FixtureCollector())

    async def lister():
        return namespaces

    return VpaSignalSource(SourceSettings(), deps(), connector=connector, namespace_lister=lister)


async def _batch(**kwargs) -> SourceBatch:
    context = kwargs.pop("context", None) or _context()
    return await _source(**kwargs).collect(collect_context(context))


def _resources(signal) -> ContainerResourcesProposal:
    assert isinstance(signal.proposal, ContainerResourcesProposal)
    return signal.proposal


def test_goldilocks_labels_are_recognized():
    goldilocks = VPARecommendation(
        "ns", "v", "Deployment", "a", "c", labels={"creator": "Fairwinds", "source": "goldilocks"}
    )
    user = VPARecommendation("ns", "v", "Deployment", "a", "c", labels={"creator": "someone"})
    assert is_goldilocks_vpa(goldilocks)
    assert not is_goldilocks_vpa(user)
    assert not is_goldilocks_vpa(VPARecommendation("ns", "v", "Deployment", "a", "c"))


async def test_collector_reads_labels_bounds_and_uncapped_target():
    vpas = await FixtureCollector().collect()

    by_name = {vpa.vpa_name: vpa for vpa in vpas}
    assert set(by_name) == {"goldilocks-api", "api-vpa"}
    goldilocks = by_name["goldilocks-api"]
    assert goldilocks.labels == {"creator": "Fairwinds", "source": "goldilocks"}
    assert goldilocks.cpu_uncapped_millicores == 300
    assert goldilocks.memory_uncapped_bytes == 512 * MIB
    assert goldilocks.cpu_upper_millicores == 320


async def test_each_vpa_object_is_one_signal_with_its_provenance():
    batch = await _batch(namespaces=["shop"])

    by_source = {}
    for signal in batch.signals:
        by_source.setdefault((signal.source_id, signal.kind), []).append(signal)
    assert set(by_source) == {
        (GOLDILOCKS_SOURCE_ID, "RIGHTSIZING_CPU"),
        (GOLDILOCKS_SOURCE_ID, "RIGHTSIZING_MEMORY"),
        ("vpa", "RIGHTSIZING_CPU"),
        ("vpa", "RIGHTSIZING_MEMORY"),
    }
    assert all(len(signals) == 1 for signals in by_source.values())
    goldilocks_cpu = by_source[(GOLDILOCKS_SOURCE_ID, "RIGHTSIZING_CPU")][0]
    assert goldilocks_cpu.source_ref == "shop/goldilocks-api"
    assert goldilocks_cpu.algorithm.name == "vpa-recommender"
    assert _resources(goldilocks_cpu).proposed.cpu_request_m == 300
    assert by_source[("vpa", "RIGHTSIZING_CPU")][0].source_ref == "shop/api-vpa"
    assert _resources(by_source[("vpa", "RIGHTSIZING_CPU")][0]).proposed.cpu_request_m == 250


async def test_goldilocks_and_user_vpa_count_as_one_engine():
    batch = await _batch(namespaces=["shop"])

    cpu = [signal for signal in batch.signals if signal.kind == "RIGHTSIZING_CPU"]
    assert len(cpu) == 2
    assert {signal.engine_family for signal in batch.signals} == {VPA_ENGINE_FAMILY}
    # One consensus vote per engine family, whatever the number of VPA objects.
    assert len({signal.engine for signal in cpu}) == 1
    assert len({signal.signal_id for signal in cpu}) == 2


async def test_v1_recommendations_are_unchanged_and_supported_by_their_signals():
    batch = await _batch(namespaces=["shop"])

    # The two VPAs disagree on CPU (no v1 recommendation, as before) and agree on memory.
    assert [rec.type for rec in batch.recommendations] == [RecommendationType.RIGHTSIZING_MEMORY]
    [memory_rec] = batch.recommendations
    assert memory_rec.source_ref == "shop/goldilocks-api"
    memory = [signal for signal in batch.signals if signal.kind == "RIGHTSIZING_MEMORY"]
    assert all(signal.provenance.legacy_fingerprint for signal in memory)
    cpu = [signal for signal in batch.signals if signal.kind == "RIGHTSIZING_CPU"]
    assert all(signal.provenance.legacy_fingerprint is None for signal in cpu)


async def test_bounds_and_volatility_travel_in_raw_and_evidence():
    batch = await _batch(namespaces=["shop"])

    goldilocks_cpu = next(
        s for s in batch.signals if s.source_id == GOLDILOCKS_SOURCE_ID and s.kind == "RIGHTSIZING_CPU"
    )
    raw = goldilocks_cpu.raw
    assert raw["provenance"] == "goldilocks"
    assert raw["lowerBound"]["cpu_millicores"] == 120
    assert raw["upperBound"]["cpu_millicores"] == 320
    assert raw["uncappedTarget"]["cpu_millicores"] == 300
    # 300m target against a 320m upper bound: the workload looks volatile.
    assert goldilocks_cpu.evidence.data["risk_hints"]["volatility"] == {"target_to_upper": 0.938, "volatile": True}
    user_cpu = next(s for s in batch.signals if s.source_id == "vpa" and s.kind == "RIGHTSIZING_CPU")
    assert user_cpu.evidence.data["risk_hints"]["volatility"]["volatile"] is False


async def test_increases_become_signals_but_not_v1_recommendations():
    batch = await _batch(context=_context(cpu_request=100, memory_request=1024 * MIB), namespaces=["shop"])

    cpu = [signal for signal in batch.signals if signal.kind == "RIGHTSIZING_CPU"]
    assert {_resources(signal).direction for signal in cpu} == {"increase"}
    assert all("increasing" in signal.evidence.summary for signal in cpu)
    assert RecommendationType.RIGHTSIZING_CPU not in {rec.type for rec in batch.recommendations}


async def test_namespace_opt_in_and_skipped_modes_are_diagnostics():
    batch = await _batch(namespaces=["shop", "payments"])

    codes = {d.code: d for d in batch.diagnostics}
    assert codes["skipped_vpas"].count == 1 and "updateMode=Auto" in codes["skipped_vpas"].message
    assert codes["goldilocks_vpas"].count == 1
    assert codes["goldilocks_namespaces"].count == 2
    missing = codes["goldilocks_namespace_without_vpas"]
    assert missing.level == "warning" and missing.count == 1 and "payments" in missing.message


async def test_unreadable_namespaces_only_skip_the_opt_in_diagnostics():
    batch = await _batch(namespaces=None)

    codes = {d.code for d in batch.diagnostics}
    assert "goldilocks_namespaces" not in codes and "goldilocks_vpas" in codes
    assert batch.signals


async def test_runner_accepts_goldilocks_signals_from_the_vpa_connector():
    report = await SourceRunner().run(_source(namespaces=["shop"]), collect_context(_context()))

    assert report.run.status == SourceRunStatus.SUCCEEDED
    assert {signal.source_id for signal in report.signals} == {"vpa", GOLDILOCKS_SOURCE_ID}


async def test_goldilocks_only_vpa_never_emits_a_vpa_signal():
    payload = json.loads(FIXTURE.read_text())
    payload["items"] = [payload["items"][0]]
    batch = await _batch(collector=FixtureCollector(payload), namespaces=["shop"])

    assert {signal.source_id for signal in batch.signals} == {GOLDILOCKS_SOURCE_ID}
    assert len(batch.signals) == 2
    assert {rec.source_ref for rec in batch.recommendations} == {"shop/goldilocks-api"}


def test_plugins_cannot_emit_other_source_ids():
    from greenkube.signals.model import Capability

    class Sneaky(Source):
        id = "sneaky"
        display_name = "Sneaky"
        capabilities = frozenset({Capability.CONTAINER_REQUESTS})
        also_emits = frozenset({"vpa"})

        async def collect(self, ctx):
            return SourceBatch()

    with pytest.raises(ValueError, match="also_emits"):
        validate_source_class(Sneaky)


class TestVpaWithGoldilocksContract(SourceContract):
    def make_source(self):
        return _source(namespaces=["shop"])

    def make_context(self):
        return collect_context(_context())
