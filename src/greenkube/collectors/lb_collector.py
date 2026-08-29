# src/greenkube/collectors/lb_collector.py
"""
Collects orphaned LoadBalancer Services from the Kubernetes API.

Used to detect Services of type ``LoadBalancer`` that no longer have any
backing endpoints, so the recommender can suggest deleting them and
stopping the cloud provider's LoadBalancer billing.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_core_v1_api

logger = logging.getLogger(__name__)


@dataclass
class OrphanedLoadBalancer:
    """Describes a LoadBalancer Service that has no live backing endpoints."""

    name: str
    namespace: str
    endpoint_count: int
    external_ip: Optional[str] = None
    ports: str = ""
    annual_cost: Optional[float] = None


class LoadBalancerCollector:
    """Collects orphaned LoadBalancer Services from the Kubernetes API.

    A Service of type ``LoadBalancer`` is considered orphaned when it has no
    ready endpoints: its selector either matches no pods or the pods behind it
    are gone. The cloud LoadBalancer provisioned for it keeps being billed by
    the hour even though nothing is routed to it.

    Services of other types, headless services, and LoadBalancers with at
    least one ready endpoint are intentionally skipped.
    """

    async def collect(self) -> List[OrphanedLoadBalancer]:
        """Fetches all Services and Endpoints and extracts the orphaned ones.

        Returns:
            A list of OrphanedLoadBalancer descriptors.
            Returns an empty list on any error (graceful degradation).
        """
        orphaned: List[OrphanedLoadBalancer] = []

        try:
            api = await get_core_v1_api()
            if not api:
                logger.debug("CoreV1 API not available; skipping LoadBalancer collection.")
                return orphaned

            service_list = await api.list_service_for_all_namespaces(
                _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None
            )
            endpoints_list = await api.list_endpoints_for_all_namespaces(
                _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None
            )

            endpoint_counts: Dict[str, int] = {}
            for endpoints in endpoints_list.items:
                namespace = endpoints.metadata.namespace
                name = endpoints.metadata.name
                if not namespace or not name:
                    continue
                count = 0
                for subset in endpoints.subsets or []:
                    count += len(subset.addresses or [])
                endpoint_counts[f"{namespace}/{name}"] = count

            for service in service_list.items:
                spec = service.spec
                if not spec or spec.type != "LoadBalancer":
                    continue

                name = service.metadata.name
                namespace = service.metadata.namespace
                if not name or not namespace:
                    continue

                endpoint_count = endpoint_counts.get(f"{namespace}/{name}", 0)
                if endpoint_count > 0:
                    continue

                external_ip = None
                if service.status and service.status.load_balancer and service.status.load_balancer.ingress:
                    first = service.status.load_balancer.ingress[0]
                    external_ip = first.ip or first.hostname

                ports = ",".join(f"{p.port}/{p.protocol or 'TCP'}" for p in (spec.ports or []) if p and p.port)

                orphaned.append(
                    OrphanedLoadBalancer(
                        name=name,
                        namespace=namespace,
                        endpoint_count=endpoint_count,
                        external_ip=external_ip,
                        ports=ports,
                    )
                )
                logger.debug(
                    "Found orphaned LoadBalancer Service '%s/%s' (endpoints=%d, external_ip=%s)",
                    namespace,
                    name,
                    endpoint_count,
                    external_ip,
                )

            logger.info("Collected %d orphaned LoadBalancer Services from cluster.", len(orphaned))
        except Exception as e:
            logger.warning(
                "Failed to collect orphaned LoadBalancer Services: %s. Continuing without LB cleanup recommendations.",
                e,
            )

        return orphaned


async def enrich_orphaned_lb_costs(
    services: List[OrphanedLoadBalancer],
    window_days: int = 7,
    opencost: Optional[OpenCostCollector] = None,
) -> List[OrphanedLoadBalancer]:
    """Attach real annualized OpenCost LoadBalancer costs when available.

    Services that OpenCost has no cost data for keep ``annual_cost=None`` so
    the recommender falls back to the flat LOAD_BALANCER_COST_PER_MONTH estimate.

    Args:
        services: The orphaned LoadBalancer descriptors to enrich (mutated in place).
        window_days: Observation window in days used for the OpenCost query;
                     the reported window cost is annualized against it.
        opencost: Optional OpenCostCollector instance (dependency injection for tests).

    Returns:
        The same list of services, enriched in place.
    """
    if not services:
        return services

    collector = opencost or OpenCostCollector()
    try:
        costs = await collector.collect_lb_costs([f"{s.namespace}/{s.name}" for s in services], window_days=window_days)
        enriched_count = 0
        if costs:
            for service in services:
                window_cost = costs.get(f"{service.namespace}/{service.name}")
                if window_cost:
                    service.annual_cost = window_cost * (365.0 / window_days)
                    enriched_count += 1
        if enriched_count:
            logger.info(
                "Attached real OpenCost costs to %d of %d orphaned LoadBalancer Services.",
                enriched_count,
                len(services),
            )
        else:
            logger.debug("OpenCost has no cost data for the orphaned LoadBalancer Services; using estimates.")
    except Exception as e:
        logger.warning("Could not enrich orphaned LoadBalancer costs from OpenCost: %s. Using estimates.", e)
    finally:
        if opencost is None:
            await collector.close()

    return services
