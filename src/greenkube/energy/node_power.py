# src/greenkube/energy/node_power.py
"""Node power-profile resolution with explicit provenance (roadmap A10, BUG-08).

Resolution order for a node at a given time:

1. ``instance_profile`` — the instance type has a known CCF profile.
2. ``node_capacity_derived`` — the node's CPU capacity (from a historical
   snapshot, the live node list, or an inferred ``cpu-N`` type) multiplied by
   the per-vCPU CCF coefficients of its provider, or their cross-provider
   average when the provider is unknown.
3. ``default_profile`` — nothing is known: ``DEFAULT_INSTANCE_PROFILE`` is used
   and the fallback is reported (reason + ``greenkube_unknown_power_profile_nodes``).

The string values match the ``PowerSource`` provenance enum of roadmap A11.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional

from ..data.instance_profiles import load_provider_estimates

logger = logging.getLogger(__name__)

POWER_SOURCE_INSTANCE_PROFILE = "instance_profile"
POWER_SOURCE_NODE_CAPACITY_DERIVED = "node_capacity_derived"
POWER_SOURCE_DEFAULT_PROFILE = "default_profile"

_PROVIDER_ALIASES = {
    "aws": "AWS",
    "gcp": "GCP",
    "google": "GCP",
    "google cloud": "GCP",
    "azure": "Azure",
    "ovh": "OVH",
    "scaleway": "Scaleway",
}


@dataclass(frozen=True)
class NodePowerProfile:
    """A power profile plus where it came from."""

    profile: dict[str, Any]
    source: str
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_fallback(self) -> bool:
        return self.source == POWER_SOURCE_DEFAULT_PROFILE

    @property
    def known_cores(self) -> Optional[float]:
        """vCPUs of the node when the profile knows them; ``None`` for ``DEFAULT_INSTANCE_PROFILE``.

        The default profile's ``vcores`` describe the fallback wattage, not the node: using
        them for embodied emissions would charge 1 vCPU of the per-vCPU fallback instead of
        the flat default (NRG-01).
        """
        if self.is_fallback:
            return None
        try:
            cores = float(self.profile.get("vcores") or 0.0)
        except (TypeError, ValueError):
            return None
        return cores if cores > 0 else None


@lru_cache(maxsize=1)
def _provider_coefficients() -> dict[str, dict[str, float]]:
    return load_provider_estimates(Path(__file__).resolve().parent.parent / "data")


def per_core_watts(provider: Optional[str]) -> tuple[float, float, str]:
    """Return ``(min_watts, max_watts, label)`` per vCPU from the CCF coefficients."""
    coefficients = _provider_coefficients()
    key = _PROVIDER_ALIASES.get((provider or "").strip().lower())
    if key and key in coefficients:
        values = coefficients[key]
        return values["min_watts"], values["max_watts"], key
    if coefficients:
        count = len(coefficients)
        min_avg = sum(v["min_watts"] for v in coefficients.values()) / count
        max_avg = sum(v["max_watts"] for v in coefficients.values()) / count
        return min_avg, max_avg, "cross-provider average"
    # CCF documented AWS defaults; only reached if the bundled CSV is missing.
    return 0.74, 3.5, "CCF default"


def default_profile_from_config(config: Any) -> dict[str, Any]:
    """``DEFAULT_INSTANCE_PROFILE`` of the configuration: used only when nothing is known of a node."""

    def _number(name: str, default: float) -> float:
        try:
            return float(getattr(config, name, default))
        except (TypeError, ValueError):
            return default

    return {
        "vcores": int(_number("DEFAULT_INSTANCE_VCORES", 1) or 1),
        "minWatts": _number("DEFAULT_INSTANCE_MIN_WATTS", 1.0),
        "maxWatts": _number("DEFAULT_INSTANCE_MAX_WATTS", 10.0),
    }


def capacity_derived_profile(cores: float, provider: Optional[str]) -> dict[str, Any]:
    min_per_core, max_per_core, _ = per_core_watts(provider)
    return {"vcores": cores, "minWatts": min_per_core * cores, "maxWatts": max_per_core * cores}


def _inferred_cores(instance_type: Optional[str]) -> Optional[float]:
    if isinstance(instance_type, str) and instance_type.startswith("cpu-"):
        try:
            cores = float(instance_type.split("-", 1)[1])
        except ValueError:
            return None
        return cores if cores > 0 else None
    return None


def resolve_node_power_profile(
    *,
    node_name: str,
    instance_type: Optional[str],
    cpu_capacity_cores: Optional[float],
    provider: Optional[str],
    instance_profiles: Mapping[str, Mapping[str, Any]],
    default_profile: Mapping[str, Any],
) -> NodePowerProfile:
    """Resolve the power profile of one node with its provenance."""
    if instance_type:
        known = instance_profiles.get(instance_type)
        if known:
            return NodePowerProfile(profile=dict(known), source=POWER_SOURCE_INSTANCE_PROFILE)

    cores = _inferred_cores(instance_type)
    if cores is None and cpu_capacity_cores is not None and cpu_capacity_cores > 0:
        cores = float(cpu_capacity_cores)
    if cores is not None:
        _, _, label = per_core_watts(provider)
        reason = (
            f"Power profile for node '{node_name}' derived from {cores:g} vCPU capacity "
            f"and {label} CCF per-vCPU coefficients"
        )
        if instance_type and not instance_type.startswith("cpu-"):
            reason += f" (unknown instance type '{instance_type}')"
        return NodePowerProfile(
            profile=capacity_derived_profile(cores, provider),
            source=POWER_SOURCE_NODE_CAPACITY_DERIVED,
            reasons=(reason,),
        )

    label = f"node '{node_name}'" if node_name else "pods without a resolvable node"
    return NodePowerProfile(
        profile=dict(default_profile),
        source=POWER_SOURCE_DEFAULT_PROFILE,
        reasons=(f"No instance type or CPU capacity for {label}; used DEFAULT_INSTANCE_PROFILE",),
    )
