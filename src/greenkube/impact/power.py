# src/greenkube/impact/power.py
"""``PowerModel`` building blocks of the Impact Model (roadmap B5).

A node's power is the sum of three components:

* :class:`LinearCpuPowerModel` — ``P_min + util × (P_max − P_min)`` from the
  node's CPU power profile (Cloud Carbon Footprint coefficients);
* :class:`MemoryPowerModel` — a constant ``W/GiB`` of installed memory
  (default 0.392 W/GiB, the Cloud Carbon Footprint memory coefficient);
* :class:`GpuPowerModel` — the power DCGM reports per GPU
  (``DCGM_FI_DEV_POWER_USAGE``), or, when only the GPU model and utilization
  are known, a linear model between an idle floor and the board power (TDP)
  from ``data/gpu_profiles.json``.

The models are pure: they turn observations into watts. Allocation of those
watts to workloads lives in :mod:`greenkube.impact.allocation`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Cloud Carbon Footprint memory coefficient (W per GB of installed memory).
DEFAULT_MEMORY_WATTS_PER_GIB = 0.392

#: Bundled GPU board-power table.
GPU_PROFILES_PATH = Path(__file__).resolve().parent.parent / "data" / "gpu_profiles.json"

#: ``gpu_source`` values recorded on node idle rows and in projections.
GPU_SOURCE_DCGM = "dcgm"
GPU_SOURCE_TDP = "gpu_tdp"


@dataclass(frozen=True)
class LinearCpuPowerModel:
    """Node CPU power, linear in CPU utilization between idle and full load."""

    idle_watts: float
    max_watts: float

    def watts(self, utilization: float) -> float:
        util = min(max(float(utilization), 0.0), 1.0)
        return self.idle_watts + util * max(self.max_watts - self.idle_watts, 0.0)


@dataclass(frozen=True)
class MemoryPowerModel:
    """Memory power, proportional to the GiB it is applied to."""

    watts_per_gib: float = DEFAULT_MEMORY_WATTS_PER_GIB

    def watts(self, gib: float) -> float:
        return max(float(gib), 0.0) * max(self.watts_per_gib, 0.0)


@dataclass(frozen=True)
class GpuProfile:
    """Board power of one GPU model."""

    name: str
    model: str
    variants: Tuple[str, ...]
    tdp_watts: float
    idle_watts: float

    def watts(self, utilization_pct: Optional[float]) -> float:
        if utilization_pct is None:
            return self.idle_watts
        util = min(max(float(utilization_pct), 0.0), 100.0) / 100.0
        return self.idle_watts + util * max(self.tdp_watts - self.idle_watts, 0.0)


@dataclass(frozen=True)
class GpuDevice:
    """One GPU observed over a window (from DCGM), possibly attributed to a pod."""

    node: str
    device: str
    model_name: Optional[str] = None
    power_watts: Optional[float] = None
    utilization_pct: Optional[float] = None
    namespace: Optional[str] = None
    pod: Optional[str] = None

    @property
    def pod_key(self) -> Optional[Tuple[str, str]]:
        if self.namespace and self.pod:
            return (self.namespace, self.pod)
        return None


def _tokens(name: str) -> List[str]:
    return [token for token in re.split(r"[^A-Z0-9]+", name.upper()) if token]


@lru_cache(maxsize=4)
def _load_profiles(path: str) -> Tuple[GpuProfile, ...]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("GPU profile table %s is unreadable: %s", path, error)
        return ()
    idle_fraction = float(document.get("idle_fraction", 0.15))
    profiles: List[GpuProfile] = []
    for entry in document.get("profiles", []) or []:
        try:
            tdp = float(entry["tdp_watts"])
            profiles.append(
                GpuProfile(
                    name=str(entry["name"]),
                    model=str(entry["model"]).upper(),
                    variants=tuple(str(variant).upper() for variant in entry.get("variants", []) or []),
                    tdp_watts=tdp,
                    idle_watts=float(entry.get("idle_watts", tdp * idle_fraction)),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.debug("Skipping malformed GPU profile entry %r", entry)
    return tuple(profiles)


class GpuPowerModel:
    """Power of one GPU: DCGM's measured power, else the TDP table."""

    def __init__(self, profiles: Optional[Sequence[GpuProfile]] = None, path: Path = GPU_PROFILES_PATH):
        self.profiles: Tuple[GpuProfile, ...] = tuple(profiles) if profiles is not None else _load_profiles(str(path))

    def profile_for(self, model_name: Optional[str]) -> Optional[GpuProfile]:
        """Matches a DCGM ``modelName`` or a ``nvidia.com/gpu.product`` label to a profile."""
        if not model_name:
            return None
        tokens = _tokens(model_name)
        for profile in self.profiles:
            if profile.model not in tokens:
                continue
            if all(any(token.startswith(variant) for token in tokens) for variant in profile.variants):
                return profile
        return None

    def device_watts(self, device: GpuDevice) -> Optional[Tuple[float, str]]:
        """Average power of a GPU over the window and its source, or None when unknown."""
        if device.power_watts is not None and device.power_watts >= 0:
            return float(device.power_watts), GPU_SOURCE_DCGM
        profile = self.profile_for(device.model_name)
        if profile is None:
            return None
        return profile.watts(device.utilization_pct), GPU_SOURCE_TDP


@dataclass(frozen=True)
class NodeGpuPower:
    """GPU power of one node over a window: attributed to pods, and unattributed."""

    by_pod: Mapping[Tuple[str, str], float]
    #: Utilization of the GPUs attributed to each pod, in GPU-millis (1000 = one fully used GPU).
    usage_by_pod: Mapping[Tuple[str, str], int]
    unattributed_watts: float
    sources: Tuple[str, ...]

    @property
    def total_watts(self) -> float:
        return sum(self.by_pod.values()) + self.unattributed_watts


#: ``estimation_reasons`` prefixes of rows of a GPU node without DCGM samples (NRG-13).
GPU_TDP_ESTIMATE_REASON_PREFIX = "GPU power estimated from TDP"
GPU_UNMEASURED_REASON_PREFIX = "GPU power not measured"


@dataclass(frozen=True)
class UndetectedGpuPower:
    """Estimated power of the GPUs of a node that DCGM does not report (NRG-13)."""

    #: Whole-node GPU power in W, or ``None`` when the GPU model is unknown.
    watts: Optional[float]
    reason: str


def undetected_gpu_power(
    gpu_count: int, gpu_model: Optional[str], model: Optional[GpuPowerModel] = None
) -> UndetectedGpuPower:
    """GPU power of a node whose capacity declares ``gpu_count`` GPUs but DCGM has no sample.

    Without utilization the GPUs are counted at the idle floor of their TDP profile
    (``GpuProfile.watts(None)``), a lower bound. Pod requests of GPUs are not attributed:
    the power goes to the node's ``__idle__`` row.
    """
    model = model or GpuPowerModel()
    profile = model.profile_for(gpu_model)
    if profile is None:
        return UndetectedGpuPower(
            watts=None,
            reason=(
                f"{GPU_UNMEASURED_REASON_PREFIX}: {gpu_count} GPU(s) of unknown model '{gpu_model or 'unknown'}' "
                "and no DCGM exporter series"
            ),
        )
    watts = gpu_count * profile.watts(None)
    return UndetectedGpuPower(
        watts=watts,
        reason=(
            f"{GPU_TDP_ESTIMATE_REASON_PREFIX}: DCGM exporter not available; {gpu_count} x {profile.name} "
            f"counted at the idle floor ({profile.watts(None):g} W each)"
        ),
    )


def node_gpu_power(devices: Sequence[GpuDevice], model: Optional[GpuPowerModel] = None) -> NodeGpuPower:
    """Sums the power of a node's GPUs, attributing each GPU to the pod DCGM names."""
    model = model or GpuPowerModel()
    by_pod: dict[Tuple[str, str], float] = {}
    usage_by_pod: dict[Tuple[str, str], int] = {}
    unattributed = 0.0
    sources: set[str] = set()
    for device in devices:
        resolved = model.device_watts(device)
        key = device.pod_key
        if key is not None and device.utilization_pct is not None:
            usage_by_pod[key] = usage_by_pod.get(key, 0) + int(round(min(max(device.utilization_pct, 0.0), 100.0) * 10))
        if resolved is None:
            continue
        watts, source = resolved
        sources.add(source)
        if key is None:
            unattributed += watts
        else:
            by_pod[key] = by_pod.get(key, 0.0) + watts
    return NodeGpuPower(
        by_pod=by_pod,
        usage_by_pod=usage_by_pod,
        unattributed_watts=unattributed,
        sources=tuple(sorted(sources)),
    )


def memory_watts_per_gib(config: Any) -> float:
    """``POWER_MEMORY_W_PER_GIB`` from a configuration object, or the CCF default."""
    value = getattr(config, "POWER_MEMORY_W_PER_GIB", DEFAULT_MEMORY_WATTS_PER_GIB)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return DEFAULT_MEMORY_WATTS_PER_GIB
    return float(value)
