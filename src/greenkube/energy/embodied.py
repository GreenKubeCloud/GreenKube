# src/greenkube/energy/embodied.py
"""Whole-node embodied emissions: one resolution for collection, recompute and projections (NRG-01).

Resolution order for one node (:func:`resolve_node_embodied`):

1. ``configured``: the node annotation ``greenkube.io/embodied-kg`` (``NodeInfo.embodied_emissions_kg``);
2. ``boavizta``: the Boavizta API value for the node's provider and instance type, cached in
   ``instance_carbon_profiles`` (see :mod:`greenkube.core.embodied_service`);
3. ``fallback_per_vcpu``: ``DEFAULT_EMBODIED_KG_PER_VCPU`` × cores + ``DEFAULT_EMBODIED_KG_PER_GIB`` × GiB;
4. ``fallback_default``: the flat ``DEFAULT_EMBODIED_EMISSIONS_KG`` when the capacity is unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Mapping, Optional

from greenkube.models.provenance import EMBODIED_FALLBACK_REASON_PREFIX, EmbodiedSource

logger = logging.getLogger(__name__)

HOURS_PER_YEAR = 8760
BYTES_PER_GIB = 1024**3

#: Node annotation carrying the operator's whole-node embodied emissions, in kg CO2e.
EMBODIED_KG_ANNOTATION = "greenkube.io/embodied-kg"
#: ``instance_carbon_profiles.source`` of a value read from the Boavizta API.
PROFILE_SOURCE_BOAVIZTA = "boavizta_api"
#: ``instance_carbon_profiles.source`` of a failed lookup (negative cache entry).
PROFILE_SOURCE_FAILED = "fallback"
#: A failed Boavizta lookup is retried at most this often.
BOAVIZTA_RETRY_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class NodeEmbodied:
    """Whole-node embodied emissions over the hardware lifespan, and where they came from."""

    gwp_kg: float
    lifespan_hours: float
    source: str

    @property
    def g_per_hour(self) -> float:
        """Embodied emissions amortized per hour of the lifespan, in g CO2e."""
        if self.gwp_kg <= 0 or self.lifespan_hours <= 0:
            return 0.0
        return self.gwp_kg * 1000.0 / self.lifespan_hours

    @property
    def is_fallback(self) -> bool:
        """True when the value is an estimate, not a Boavizta or operator value."""
        return self.source in (EmbodiedSource.FALLBACK_PER_VCPU.value, EmbodiedSource.FALLBACK_DEFAULT.value)

    def reason(self, node_name: str) -> Optional[str]:
        """The estimation reason of rows using this value, or ``None`` for a real value."""
        if not self.is_fallback:
            return None
        return (
            f"{EMBODIED_FALLBACK_REASON_PREFIX} for node '{node_name}' ({self.source}: {self.gwp_kg:.1f} kg CO2e); "
            f"no Boavizta value for its instance type and no {EMBODIED_KG_ANNOTATION} annotation"
        )


#: Boavizta profiles known to this process (filled by collection and recompute), read by the
#: projector so a projection uses the embodied value the rows it is compared with use (NRG-05).
_KNOWN_PROFILES: Dict[tuple, Dict[str, Any]] = {}


def remember_profiles(profiles: Mapping[tuple, Mapping[str, Any]]) -> None:
    """Record Boavizta profiles for :func:`known_profiles`."""
    for key, profile in profiles.items():
        _KNOWN_PROFILES[key] = dict(profile)


def known_profiles() -> Mapping[tuple, Mapping[str, Any]]:
    """Boavizta profiles this process has read or fetched (empty before the first collection)."""
    return _KNOWN_PROFILES


def _setting(config: Any, name: str, default: float) -> float:
    value = getattr(config, name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def lifespan_hours(config: Any) -> float:
    """Hardware lifespan in hours (``DEFAULT_HARDWARE_LIFESPAN_YEARS`` × 8760)."""
    return _setting(config, "DEFAULT_HARDWARE_LIFESPAN_YEARS", 4.0) * HOURS_PER_YEAR


def parse_embodied_annotation(annotations: Optional[Mapping[str, str]]) -> Optional[float]:
    """The positive kg value of the ``greenkube.io/embodied-kg`` annotation, or ``None``."""
    if not isinstance(annotations, Mapping):
        return None
    raw = annotations.get(EMBODIED_KG_ANNOTATION)
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring %s=%r: not a number of kg CO2e.", EMBODIED_KG_ANNOTATION, raw)
        return None
    if not value > 0 or value == float("inf"):
        logger.warning("Ignoring %s=%r: must be a positive number of kg CO2e.", EMBODIED_KG_ANNOTATION, raw)
        return None
    return value


def resolve_node_embodied(
    info: Any,
    profiles: Mapping[tuple, Mapping[str, Any]],
    config: Any,
    *,
    cpu_capacity_cores: Optional[float] = None,
) -> NodeEmbodied:
    """Embodied emissions of one node: configured, Boavizta, per-vCPU fallback, then flat default.

    ``info`` is a :class:`~greenkube.models.node.NodeInfo` (or ``None``); ``profiles`` maps
    ``(provider, instance_type)`` to Boavizta profiles (``gwp_manufacture``, ``lifespan_hours``)
    as returned by ``EmbodiedEmissionsService.prepare_embodied_data`` or
    :func:`load_cached_profiles`. ``cpu_capacity_cores`` is used when ``info`` has no capacity.
    """
    hours = lifespan_hours(config)
    configured = getattr(info, "embodied_emissions_kg", None) if info is not None else None
    if configured is not None and configured > 0:
        return NodeEmbodied(float(configured), hours, EmbodiedSource.CONFIGURED.value)
    provider = getattr(info, "cloud_provider", None) if info is not None else None
    instance_type = getattr(info, "instance_type", None) if info is not None else None
    if provider and instance_type:
        profile = profiles.get((provider, instance_type))
        if profile and not profile.get("is_fallback"):
            gwp = profile.get("gwp_manufacture")
            if gwp is not None and float(gwp) > 0:
                profile_hours = profile.get("lifespan_hours") or hours
                return NodeEmbodied(float(gwp), float(profile_hours), EmbodiedSource.BOAVIZTA.value)
    cores = getattr(info, "cpu_capacity_cores", None) if info is not None else None
    if not cores:
        cores = cpu_capacity_cores
    per_vcpu = _setting(config, "DEFAULT_EMBODIED_KG_PER_VCPU", 12.0)
    if cores is not None and cores > 0 and per_vcpu > 0:
        memory_bytes = (getattr(info, "memory_capacity_bytes", None) or 0) if info is not None else 0
        per_gib = max(_setting(config, "DEFAULT_EMBODIED_KG_PER_GIB", 0.0), 0.0)
        gwp = float(cores) * per_vcpu + memory_bytes / BYTES_PER_GIB * per_gib
        return NodeEmbodied(gwp, hours, EmbodiedSource.FALLBACK_PER_VCPU.value)
    return NodeEmbodied(
        _setting(config, "DEFAULT_EMBODIED_EMISSIONS_KG", 100.0), hours, EmbodiedSource.FALLBACK_DEFAULT.value
    )


def usable_profile(stored: Optional[Mapping[str, Any]], default_hours: float) -> Optional[Dict[str, Any]]:
    """A stored ``instance_carbon_profiles`` row as a Boavizta profile, or ``None`` for a failed lookup."""
    if not stored or stored.get("source") == PROFILE_SOURCE_FAILED:
        return None
    gwp = stored.get("gwp_manufacture")
    if gwp is None or float(gwp) <= 0:
        return None
    return {
        "gwp_manufacture": float(gwp),
        "lifespan_hours": float(stored.get("lifespan_hours") or default_hours),
        "source": EmbodiedSource.BOAVIZTA.value,
    }


async def load_cached_profiles(embodied_repository: Any, nodes: Mapping[str, Any], config: Any) -> Dict[tuple, dict]:
    """Boavizta profiles already stored for the instance types of ``nodes``, without any HTTP call.

    Used by ``greenkube recompute`` so it resolves embodied emissions like the collection did.
    Failed lookups (negative cache entries) are left out.
    """
    cache: Dict[tuple, dict] = {}
    if embodied_repository is None:
        return cache
    keys = {
        (info.cloud_provider, info.instance_type)
        for info in nodes.values()
        if getattr(info, "cloud_provider", None) and getattr(info, "instance_type", None)
    }
    for provider, itype in sorted(keys):
        try:
            stored = await embodied_repository.get_profile(provider, itype)
        except Exception as exc:  # noqa: BLE001 - a missing profile falls back per node, flagged
            logger.warning("Could not read the embodied profile of %s/%s: %s", provider, itype, exc)
            continue
        profile = usable_profile(stored, lifespan_hours(config))
        if profile is not None:
            cache[(provider, itype)] = profile
    remember_profiles(cache)
    return cache
