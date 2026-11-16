"""Projections and collected rows use one node power model (NRG-05)."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenkube.core.config import Config
from greenkube.data.instance_profiles import INSTANCE_PROFILES
from greenkube.energy.node_power import resolve_node_power_profile
from greenkube.impact.projector import ImpactProjector
from greenkube.impact.rows import MetricRowAllocator, NodeSpec
from greenkube.models.node import NodeInfo
from greenkube.models.provenance import PowerSource

CASES = [
    pytest.param("m5.large", "aws", 2.0, id="known-type"),
    pytest.param("c7i.xlarge", "aws", 4.0, id="unknown-type-aws"),
    pytest.param("Standard_D4ds_v5", "azure", 4.0, id="unknown-type-azure"),
    pytest.param("n4-standard-8", "gcp", 8.0, id="unknown-type-gcp"),
    pytest.param("custom-6", "unknown", 6.0, id="unknown-provider"),
    pytest.param("cpu-4", "unknown", None, id="cpu-N"),
    pytest.param(None, "unknown", None, id="nothing"),
]


def _default_profile(config: Config) -> dict:
    return {
        "vcores": int(config.DEFAULT_INSTANCE_VCORES),
        "minWatts": float(config.DEFAULT_INSTANCE_MIN_WATTS),
        "maxWatts": float(config.DEFAULT_INSTANCE_MAX_WATTS),
    }


@pytest.mark.parametrize(("instance_type", "provider", "cores"), CASES)
def test_projector_and_collection_resolve_the_same_power_profile(instance_type, provider, cores):
    config = Config()
    collection = resolve_node_power_profile(
        node_name="n1",
        instance_type=instance_type,
        cpu_capacity_cores=cores,
        provider=provider,
        instance_profiles=INSTANCE_PROFILES,
        default_profile=_default_profile(config),
    )
    spec = NodeSpec(
        name="n1",
        instance_type=instance_type,
        provider=provider,
        cpu_capacity_cores=cores,
        idle_watts=float(collection.profile["minWatts"]),
        max_watts=float(collection.profile["maxWatts"]),
        vcores=float(collection.profile["vcores"]),
        power_source=collection.source,
    )
    collected = MetricRowAllocator(config).node_profile(spec, [], pue=1.0, intensity=100.0, hours=1.0)

    node = NodeInfo(name="n1", instance_type=instance_type, cloud_provider=provider, cpu_capacity_cores=cores)
    projected = ImpactProjector(config, node_infos=[node]).pool.nodes["n1"]

    assert projected.idle_watts == pytest.approx(collected.idle_watts, abs=1e-12)
    assert projected.max_watts == pytest.approx(collected.max_watts, abs=1e-12)
    assert projected.power_source == collected.power_source


def test_unknown_provider_idle_projection_uses_the_ccf_average():
    """6-core node, provider unknown, 0.1-core cut for a year (acceptance b)."""
    config = Config()
    node = NodeInfo(name="n1", instance_type="custom-6", cloud_provider="unknown", cpu_capacity_cores=6)
    profile = ImpactProjector(config, node_infos=[node]).pool.nodes["n1"]
    assert profile.power_source == PowerSource.NODE_CAPACITY_DERIVED
    assert profile.idle_watts / 6 == pytest.approx(0.726, abs=5e-4)


def test_a_single_implementation_of_profile_selection():
    src = Path(__file__).resolve().parents[2] / "src" / "greenkube"
    offenders = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "DEFAULT_INSTANCE_MIN_WATTS / " in path.read_text(encoding="utf-8")
        or "def _power_profile(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
