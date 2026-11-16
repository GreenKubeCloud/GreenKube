"""Embodied emissions from the real Boavizta v1 API, cached failures and provenance (NRG-01, NRG-07).

The recorded payload ``tests/fixtures/boavizta/aws_m5_large.json`` is the answer of
``GET https://api.boavizta.org/v1/cloud/instance?provider=aws&instance_type=m5.large&verbose=false``.
No test calls the real API: requests go to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from greenkube.collectors.boavizta_collector import BoaviztaCollector
from greenkube.collectors.node_collector import NodeCollector
from greenkube.core.config import Config
from greenkube.core.embodied_service import EmbodiedEmissionsService
from greenkube.energy.embodied import resolve_node_embodied
from greenkube.impact.rows import MetricRowAllocator, NodeSpec
from greenkube.models.boavizta import BoaviztaResponse
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo
from greenkube.models.provenance import EMBODIED_FALLBACK_REASON_PREFIX, EmbodiedSource

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "boavizta"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
LIFESPAN_HOURS = 4 * 8760


def _payload(name: str = "aws_m5_large.json") -> dict:
    return json.loads((FIXTURES / name).read_text())


class InMemoryEmbodiedRepository:
    """The ``instance_carbon_profiles`` contract, in memory, with a clock."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.save_calls = 0
        self.now = NOW

    async def get_profile(self, provider: str, instance_type: str) -> Optional[dict]:
        row = self.rows.get((provider, instance_type))
        return dict(row) if row else None

    async def save_profile(self, provider, instance_type, gwp, lifespan, source="boavizta_api", error=None):
        self.save_calls += 1
        self.rows[(provider, instance_type)] = {
            "gwp_manufacture": gwp,
            "lifespan_hours": lifespan,
            "source": source,
            "last_updated": self.now,
            "error": error,
        }


def _collector(handler) -> tuple[BoaviztaCollector, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    collector = BoaviztaCollector()
    collector.api_url = "https://boavizta.test"
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return collector, requests


def _service(collector, repository, config: Optional[Config] = None) -> tuple[EmbodiedEmissionsService, MagicMock]:
    node_repository = MagicMock()
    node_repository.save_nodes = AsyncMock(return_value=0)
    service = EmbodiedEmissionsService(
        boavizta_collector=collector,
        embodied_repository=repository,  # type: ignore[arg-type]
        node_repository=node_repository,
        calculator=MagicMock(),
        estimator=MagicMock(),
        config=config or Config(),
    )
    return service, node_repository


def test_model_parses_the_recorded_v1_payload():
    response = BoaviztaResponse.model_validate(_payload())
    assert response.embedded_kg() == 24.0


def test_model_accepts_the_verbose_object_and_rejects_another_unit():
    payload = _payload()
    payload["verbose"] = {"vcpu": {"value": 2}}
    assert BoaviztaResponse.model_validate(payload).embedded_kg() == 24.0
    payload["impacts"]["gwp"]["unit"] = "gCO2eq"
    assert BoaviztaResponse.model_validate(payload).embedded_kg() is None


def test_model_matches_the_cloud_instance_contract_of_the_openapi_spec():
    """The parameters the collector sends exist in the Boavizta OpenAPI spec (v1.3.11)."""
    spec_path = Path(__file__).resolve().parents[2] / "api.boavizta.org-openapi.json"
    if not spec_path.exists():
        pytest.skip("api.boavizta.org-openapi.json is a local-only file")
    spec = json.loads(spec_path.read_text())
    params = {p["name"] for p in spec["paths"]["/v1/cloud/instance"]["get"]["parameters"]}
    assert {"provider", "instance_type", "verbose", "criteria"} <= params


async def test_collector_returns_24_kg_for_the_recorded_m5_large_payload():
    collector, requests = _collector(lambda request: httpx.Response(200, json=_payload()))
    impact = await collector.get_server_impact(provider="aws", instance_type="m5.large")
    assert impact is not None and impact.embedded_kg() == 24.0
    assert len(requests) == 1
    assert requests[0].url.params["verbose"] == "false"
    assert "duration" not in requests[0].url.params


async def test_prepare_caches_the_boavizta_value_and_a_second_call_makes_no_request():
    collector, requests = _collector(lambda request: httpx.Response(200, json=_payload()))
    repository = InMemoryEmbodiedRepository()
    service, node_repository = _service(collector, repository)
    nodes = {"n1": NodeInfo(name="n1", cloud_provider="aws", instance_type="m5.large", cpu_capacity_cores=2)}

    cache = await service.prepare_embodied_data(nodes, now=NOW)
    assert cache[("aws", "m5.large")]["gwp_manufacture"] == 24.0
    assert not cache[("aws", "m5.large")].get("is_fallback")
    assert repository.save_calls == 1
    assert len(requests) == 1

    again = await service.prepare_embodied_data(nodes, now=NOW + timedelta(minutes=5))
    assert again[("aws", "m5.large")]["gwp_manufacture"] == 24.0
    assert len(requests) == 1, "a cached profile is never fetched again"
    # NRG-07: the embodied service never writes node snapshots nor mutates NodeInfo.
    node_repository.save_nodes.assert_not_called()
    assert nodes["n1"].embodied_emissions_kg is None

    embodied = resolve_node_embodied(nodes["n1"], cache, Config())
    assert (embodied.gwp_kg, embodied.source) == (24.0, EmbodiedSource.BOAVIZTA.value)


async def test_unreachable_api_falls_back_per_vcpu_and_retries_once_per_day():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    collector, requests = _collector(unreachable)
    repository = InMemoryEmbodiedRepository()
    service, _ = _service(collector, repository)
    nodes = {"big": NodeInfo(name="big", cloud_provider="aws", instance_type="m5.24xlarge", cpu_capacity_cores=96)}

    cache = await service.prepare_embodied_data(nodes, now=NOW)
    assert cache == {}
    assert repository.rows[("aws", "m5.24xlarge")]["source"] == "fallback"
    assert "unreachable" in repository.rows[("aws", "m5.24xlarge")]["error"]
    embodied = resolve_node_embodied(nodes["big"], cache, Config())
    assert embodied.source == EmbodiedSource.FALLBACK_PER_VCPU.value
    assert embodied.gwp_kg == pytest.approx(96 * Config().DEFAULT_EMBODIED_KG_PER_VCPU)
    reason = embodied.reason("big")
    assert reason is not None and reason.startswith(EMBODIED_FALLBACK_REASON_PREFIX)

    for minutes in (5, 60, 23 * 60):
        await service.prepare_embodied_data(nodes, now=NOW + timedelta(minutes=minutes))
    assert len(requests) == 1, "a failed lookup is retried at most every 24 h"

    await service.prepare_embodied_data(nodes, now=NOW + timedelta(hours=24, minutes=1))
    assert len(requests) == 2


async def test_on_prem_nodes_make_no_request():
    collector, requests = _collector(lambda request: httpx.Response(200, json=_payload()))
    service, _ = _service(collector, InMemoryEmbodiedRepository())
    nodes = {"m": NodeInfo(name="m", cloud_provider="unknown", instance_type="cpu-4", cpu_capacity_cores=4)}
    assert await service.prepare_embodied_data(nodes, now=NOW) == {}
    assert requests == []


def test_annotated_node_is_configured_and_wins_over_boavizta():
    info = NodeInfo(
        name="m", cloud_provider="aws", instance_type="m5.large", cpu_capacity_cores=2, embodied_emissions_kg=1500
    )
    cache = {("aws", "m5.large"): {"gwp_manufacture": 24.0, "lifespan_hours": LIFESPAN_HOURS}}
    embodied = resolve_node_embodied(info, cache, Config())
    assert (embodied.gwp_kg, embodied.source) == (1500.0, EmbodiedSource.CONFIGURED.value)
    assert embodied.g_per_hour == pytest.approx(1500 * 1000 / LIFESPAN_HOURS)
    assert embodied.reason("m") is None


def test_unknown_capacity_uses_the_flat_default_flagged():
    embodied = resolve_node_embodied(NodeInfo(name="x"), {}, Config())
    assert embodied.source == EmbodiedSource.FALLBACK_DEFAULT.value
    assert embodied.gwp_kg == Config().DEFAULT_EMBODIED_EMISSIONS_KG


async def test_node_collector_reads_the_embodied_annotation():
    node = SimpleNamespace(
        metadata=SimpleNamespace(
            name="m1",
            labels={"kubernetes.io/arch": "amd64"},
            annotations={"greenkube.io/embodied-kg": "1500"},
        ),
        spec=SimpleNamespace(provider_id=None),
        status=SimpleNamespace(capacity={"cpu": "8", "memory": "32Gi"}, node_info=None),
    )
    api = MagicMock()
    api.list_node = AsyncMock(return_value=SimpleNamespace(items=[node], metadata=SimpleNamespace(_continue=None)))
    collector = NodeCollector()
    collector._api = api
    nodes = await collector.collect()
    assert nodes["m1"].embodied_emissions_kg == 1500.0


def _row(pod: str, request_m: int) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace="ns",
        node="n1",
        timestamp=NOW,
        duration_seconds=3600,
        cpu_request=request_m,
        memory_request=0,
        cpu_usage_millicores=request_m,
    )


@pytest.mark.parametrize(
    ("source", "reason", "flagged"),
    [
        (EmbodiedSource.FALLBACK_PER_VCPU.value, "Embodied emissions estimated for node 'n1'", True),
        (EmbodiedSource.BOAVIZTA.value, None, False),
    ],
)
def test_allocated_rows_and_idle_row_carry_the_embodied_source(source, reason, flagged):
    allocator = MetricRowAllocator(Config())
    spec = NodeSpec(
        name="n1",
        cpu_capacity_cores=4,
        idle_watts=4,
        max_watts=10,
        vcores=4,
        embodied_g_per_hour=10.0,
        embodied_source=source,
        embodied_reason=reason,
    )
    rows = [_row("a", 1000)]
    profile = allocator.node_profile(spec, rows, pue=1.0, intensity=100.0, hours=1.0)
    window = allocator.allocate(rows, profile, 1.0, emit_idle=True, idle_fields={"timestamp": NOW})
    pod = window.rows[0]
    assert pod.embodied_source == source
    assert any(r.startswith(EMBODIED_FALLBACK_REASON_PREFIX) for r in pod.estimation_reasons) is flagged
    assert window.idle is not None and window.idle.embodied_source == source
    # A second allocation (recompute) keeps one embodied reason, never two.
    again = allocator.allocate([pod], profile, 1.0, emit_idle=False).rows[0]
    assert sum(r.startswith(EMBODIED_FALLBACK_REASON_PREFIX) for r in again.estimation_reasons) == int(flagged)
