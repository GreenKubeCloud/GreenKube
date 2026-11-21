# src/greenkube/core/embodied_service.py
"""Boavizta embodied-emissions profiles: lookups and their cache (NRG-01).

The per-node resolution (configured, Boavizta, per-vCPU fallback, flat default) lives in
:mod:`greenkube.energy.embodied`. A failed lookup is cached as a ``fallback`` profile with its
error and retried at most every :data:`BOAVIZTA_RETRY_INTERVAL`, never on every collection cycle.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from ..collectors.boavizta_collector import BoaviztaCollector, is_cloud_provider
from ..core.calculator import CarbonCalculator
from ..core.config import Config, get_config
from ..energy.embodied import (
    BOAVIZTA_RETRY_INTERVAL,
    PROFILE_SOURCE_BOAVIZTA,
    PROFILE_SOURCE_FAILED,
    NodeEmbodied,
    lifespan_hours,
    remember_profiles,
    resolve_node_embodied,
    usable_profile,
)
from ..energy.estimator import BasicEstimator
from ..models.node import NodeInfo
from ..models.provenance import EmbodiedSource
from ..storage.base_repository import NodeRepository
from ..storage.embodied_repository import EmbodiedRepository
from ..utils.time import ensure_utc, parse_iso_date

logger = logging.getLogger(__name__)

__all__ = [
    "BOAVIZTA_RETRY_INTERVAL",
    "EmbodiedEmissionsService",
    "NodeEmbodied",
    "resolve_node_embodied",
]


def _attempted_at(value: Any) -> Optional[datetime]:
    """``last_updated`` of a stored profile (datetime or ISO string) as aware UTC, or ``None``."""
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        return parse_iso_date(value)
    return None


class EmbodiedEmissionsService:
    """Fetches and caches Boavizta embodied-emissions profiles."""

    def __init__(
        self,
        boavizta_collector: BoaviztaCollector,
        embodied_repository: EmbodiedRepository,
        node_repository: NodeRepository,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
        config: Config | None = None,
    ):
        self.boavizta_collector = boavizta_collector
        self.embodied_repository = embodied_repository
        # Node snapshots belong to the node-analysis loop; this service never writes them (NRG-07).
        self.node_repository = node_repository
        self.calculator = calculator
        self.estimator = estimator
        self._config = config if config is not None else get_config()

    async def prepare_embodied_data(
        self,
        nodes_info: Dict[str, NodeInfo],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[tuple, dict]:
        """Boavizta profiles of the cloud instance types of ``nodes_info``.

        Returns ``{(provider, instance_type): {"gwp_manufacture", "lifespan_hours", "source"}}``
        for the types Boavizta knows. Types it does not know, and failed lookups, are absent:
        :func:`resolve_node_embodied` then falls back per node and the rows are flagged. A failure
        is stored as a negative cache entry and retried after :data:`BOAVIZTA_RETRY_INTERVAL`.
        Node snapshots and ``NodeInfo`` objects are left untouched (NRG-07).
        """
        now = now or datetime.now(timezone.utc)
        unique_types = {
            (info.cloud_provider, info.instance_type)
            for info in (nodes_info or {}).values()
            if info.cloud_provider and info.instance_type and is_cloud_provider(info.cloud_provider)
        }
        hours = lifespan_hours(self._config)
        cache: Dict[tuple, dict] = {}
        to_fetch: list[tuple[str, str]] = []
        for provider, itype in sorted(unique_types):
            stored = await self.embodied_repository.get_profile(provider, itype)
            profile = usable_profile(stored, hours)
            if profile is not None:
                cache[(provider, itype)] = profile
                continue
            if stored and stored.get("source") == PROFILE_SOURCE_FAILED:
                failed_at = _attempted_at(stored.get("last_updated"))
                if failed_at is not None and now - failed_at < BOAVIZTA_RETRY_INTERVAL:
                    continue
            to_fetch.append((provider, itype))

        async def _fetch(provider: str, instance_type: str) -> Optional[tuple]:
            error = "Boavizta returned no embodied value"
            try:
                lookup = await self.boavizta_collector.lookup_server_impact(
                    provider=provider, instance_type=instance_type, verbose=False
                )
                impact = lookup.response
                gwp_kg = impact.embedded_kg() if impact is not None else None
                if gwp_kg is not None:
                    await self.embodied_repository.save_profile(
                        provider=provider,
                        instance_type=instance_type,
                        gwp=gwp_kg,
                        lifespan=int(hours),
                        source=PROFILE_SOURCE_BOAVIZTA,
                    )
                    return (provider, instance_type), {
                        "gwp_manufacture": gwp_kg,
                        "lifespan_hours": hours,
                        "source": EmbodiedSource.BOAVIZTA.value,
                    }
                if impact is None:
                    error = lookup.error or "Boavizta lookup failed"
            except Exception as exc:  # noqa: BLE001 - a lookup failure must never stop collection
                error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Boavizta profile unavailable for %s/%s (%s); embodied emissions use the per-vCPU fallback "
                "(rows flagged embodied_source=fallback_per_vcpu), next attempt in %s.",
                provider,
                instance_type,
                error,
                BOAVIZTA_RETRY_INTERVAL,
            )
            try:
                await self.embodied_repository.save_profile(
                    provider=provider,
                    instance_type=instance_type,
                    gwp=0.0,
                    lifespan=int(hours),
                    source=PROFILE_SOURCE_FAILED,
                    error=str(error)[:500],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not cache the failed Boavizta lookup of %s/%s: %s", provider, instance_type, exc)
            return None

        if to_fetch:
            logger.info("Fetching %s missing Boavizta profiles from API...", len(to_fetch))
            for result in await asyncio.gather(*(_fetch(p, i) for p, i in to_fetch)):
                if result:
                    key, value = result
                    cache[key] = value
        remember_profiles(cache)
        return cache

    def resolve(self, info: Optional[NodeInfo], boavizta_cache: Mapping[tuple, Mapping[str, Any]]) -> NodeEmbodied:
        """Embodied emissions of one node (see :func:`resolve_node_embodied`)."""
        return resolve_node_embodied(info, boavizta_cache, self._config)

    def is_embodied_fallback(
        self,
        node_info: Optional[NodeInfo],
        boavizta_cache: Dict[tuple, dict],
    ) -> bool:
        """Return True if the embodied value of this node is an estimate (per-vCPU or flat default)."""
        if not node_info:
            return False
        return self.resolve(node_info, boavizta_cache).is_fallback

    def calculate_pod_embodied(
        self,
        node_info: Optional[NodeInfo],
        boavizta_cache: Dict[tuple, dict],
        pod_requests: dict,
        cpu_usage_millicores: Optional[float] = None,
    ) -> float:
        """Calculate embodied emissions share for a single pod (legacy helper, no production caller).

        The share is computed from ``max(cpu_requests, cpu_usage)`` in millicores
        of the node's resolved embodied emissions (:func:`resolve_node_embodied`).
        """
        if not node_info:
            return 0.0
        try:
            embodied = self.resolve(node_info, boavizta_cache)
            node_capacity = 0.0
            if node_info.cpu_capacity_cores:
                node_capacity = node_info.cpu_capacity_cores
            elif node_info.instance_type:
                prof = self.estimator.instance_profiles.get(node_info.instance_type)
                if prof:
                    node_capacity = prof["vcores"]
            effective_cpu_millicores = max(pod_requests["cpu"] or 0.0, cpu_usage_millicores or 0.0)
            cpu_share = 0.0
            if node_capacity > 0 and effective_cpu_millicores > 0:
                cpu_share = min((effective_cpu_millicores / 1000.0) / node_capacity, 1.0)
            if cpu_share == 0.0:
                return 0.0
            return self.calculator.calculate_embodied_emissions(
                gwp_manufacture_kg=embodied.gwp_kg,
                lifespan_hours=embodied.lifespan_hours,
                duration_seconds=self.estimator.query_range_step_sec,
                share=cpu_share,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Error calculating embodied emissions for %s: %s", node_info.name, e)
            return 0.0
