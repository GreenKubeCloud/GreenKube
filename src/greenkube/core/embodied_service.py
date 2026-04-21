# src/greenkube/core/embodied_service.py
"""Manages Boavizta embodied-emissions profiles: fetch, cache, and per-pod calculations."""

import asyncio
import logging
from typing import Dict, Optional

from ..collectors.boavizta_collector import BoaviztaCollector
from ..core.calculator import CarbonCalculator
from ..core.config import Config, get_config
from ..energy.estimator import BasicEstimator
from ..models.node import NodeInfo
from ..storage.base_repository import NodeRepository
from ..storage.embodied_repository import EmbodiedRepository

logger = logging.getLogger(__name__)


class EmbodiedEmissionsService:
    """Fetches, caches and calculates Boavizta embodied emissions."""

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
        self.node_repository = node_repository
        self.calculator = calculator
        self.estimator = estimator
        self._config = config if config is not None else get_config()

    async def prepare_embodied_data(
        self,
        nodes_info: Dict[str, NodeInfo],
    ) -> Dict[tuple, dict]:
        """Fetch / cache Boavizta embodied-emissions profiles.

        Returns:
            A dict mapping ``(provider, instance_type)`` to profile dicts.
        """
        unique_nodes = set()
        if nodes_info:
            for _name, info in nodes_info.items():
                if info.cloud_provider and info.instance_type:
                    unique_nodes.add((info.cloud_provider, info.instance_type))

        boavizta_cache: Dict[tuple, dict] = {}
        missing_in_db = []
        for provider, itype in unique_nodes:
            profile = await self.embodied_repository.get_profile(provider, itype)
            if profile:
                boavizta_cache[(provider, itype)] = profile
            else:
                missing_in_db.append((provider, itype))

        async def _fetch_and_save(provider: str, instance_type: str):
            try:
                impact = await self.boavizta_collector.get_server_impact(
                    provider=provider, instance_type=instance_type, verbose=True
                )
                if impact and impact.impacts and impact.impacts.gwp and impact.impacts.gwp.manufacture:
                    gwp_embedded_kg = impact.impacts.gwp.manufacture
                    if gwp_embedded_kg:
                        lifespan = self._config.DEFAULT_HARDWARE_LIFESPAN_YEARS * 8760
                        await self.embodied_repository.save_profile(
                            provider=provider,
                            instance_type=instance_type,
                            gwp=gwp_embedded_kg,
                            lifespan=lifespan,
                        )
                        return (provider, instance_type), {
                            "gwp_manufacture": gwp_embedded_kg,
                            "lifespan_hours": lifespan,
                        }
            except Exception as e:
                logger.warning(
                    "Failed to fetch/save Boavizta profile for %s/%s: %s",
                    provider,
                    instance_type,
                    e,
                )
            return None

        if missing_in_db:
            logger.info(
                "Fetching %s missing Boavizta profiles from API...",
                len(missing_in_db),
            )
            results = await asyncio.gather(*(_fetch_and_save(p, i) for p, i in missing_in_db))
            for res in results:
                if res:
                    key, val = res
                    boavizta_cache[key] = val

            # Inject fallback profiles for nodes still missing after API fetch
            for provider, itype in missing_in_db:
                key = (provider, itype)
                if key not in boavizta_cache:
                    fallback_gwp = self._config.DEFAULT_EMBODIED_EMISSIONS_KG
                    fallback_lifespan = self._config.DEFAULT_HARDWARE_LIFESPAN_YEARS * 8760
                    boavizta_cache[key] = {
                        "gwp_manufacture": fallback_gwp,
                        "lifespan_hours": fallback_lifespan,
                        "is_fallback": True,
                    }
                    logger.warning(
                        "Boavizta profile unavailable for %s/%s. Using fallback embodied emissions: %.1f kg CO2e.",
                        provider,
                        itype,
                        fallback_gwp,
                    )

        # Update NodeInfo with embodied emissions and save snapshots
        if nodes_info:
            for _node_name, info in nodes_info.items():
                if info.cloud_provider and info.instance_type:
                    key = (info.cloud_provider, info.instance_type)
                    profile = boavizta_cache.get(key)
                    if profile:
                        info.embodied_emissions_kg = profile.get("gwp_manufacture")

            try:
                await self.node_repository.save_nodes(list(nodes_info.values()))
                logger.info("Saved %d node snapshots.", len(nodes_info))
            except Exception as e:
                logger.warning("Failed to save node snapshots: %s", e)

        return boavizta_cache

    def is_embodied_fallback(
        self,
        node_info: Optional[NodeInfo],
        boavizta_cache: Dict[tuple, dict],
    ) -> bool:
        """Return True if the embodied profile for this node is a fallback estimate."""
        if not node_info or not node_info.cloud_provider or not node_info.instance_type:
            return False
        key = (node_info.cloud_provider, node_info.instance_type)
        profile = boavizta_cache.get(key)
        return bool(profile and profile.get("is_fallback"))

    def calculate_pod_embodied(
        self,
        node_info: Optional[NodeInfo],
        boavizta_cache: Dict[tuple, dict],
        pod_requests: dict,
        cpu_usage_millicores: Optional[float] = None,
    ) -> float:
        """Calculate embodied emissions share for a single pod.

        The share is computed from ``max(cpu_requests, cpu_usage)`` in millicores:
        - CPU requests represent the permanently reserved hardware slice (standard
          Scope 3 methodology — Boavizta / Cloud Carbon Footprint).
        - When requests are unset (0), actual CPU usage is used instead so that
          pods without resource requests (common in dev/Minikube) still receive
          a non-zero embodied allocation.
        """
        if not node_info or not node_info.cloud_provider or not node_info.instance_type:
            return 0.0

        key = (node_info.cloud_provider, node_info.instance_type)
        profile = boavizta_cache.get(key)
        if not profile:
            return 0.0

        try:
            gwp_kg = profile.get("gwp_manufacture")
            lifespan = profile.get("lifespan_hours")

            node_capacity = 0
            if node_info.cpu_capacity_cores:
                node_capacity = node_info.cpu_capacity_cores
            elif node_info.instance_type:
                prof = self.estimator.instance_profiles.get(node_info.instance_type)
                if prof:
                    node_capacity = prof["vcores"]

            # Embodied share is based on the larger of CPU requests or actual usage.
            # CPU requests represent the reserved hardware slice (standard methodology).
            # When requests are not set (e.g., dev/Minikube), actual usage serves as
            # the allocation basis so that embodied emissions are not silently zeroed.
            effective_cpu_millicores = max(pod_requests["cpu"] or 0.0, cpu_usage_millicores or 0.0)

            cpu_share = 0.0
            if node_capacity > 0 and effective_cpu_millicores > 0:
                cpu_share = (effective_cpu_millicores / 1000.0) / node_capacity
                cpu_share = min(cpu_share, 1.0)

            if cpu_share == 0.0:
                return 0.0

            duration = self.estimator.query_range_step_sec

            return self.calculator.calculate_embodied_emissions(
                gwp_manufacture_kg=gwp_kg,
                lifespan_hours=lifespan,
                duration_seconds=duration,
                share=cpu_share,
            )
        except Exception as e:
            logger.warning(
                "Error calculating embodied emissions for %s: %s",
                node_info.name,
                e,
            )
            return 0.0
