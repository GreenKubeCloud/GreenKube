# src/greenkube/core/optimization/context_builder.py
"""Builds an OptimizationContext from repositories and cluster collectors.

This module centralizes the data loading that used to be duplicated across the
API router, the startup scan and the CLI command.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional

from greenkube.collectors.hpa_collector import HPACollector
from greenkube.collectors.lb_collector import LoadBalancerCollector, enrich_orphaned_lb_costs
from greenkube.collectors.pv_collector import PVCollector, enrich_orphaned_pv_costs
from greenkube.core.optimization.context import OptimizationContext
from greenkube.models.metrics import CombinedMetric

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository

logger = logging.getLogger(__name__)


async def get_active_k8s_namespaces() -> Optional[set[str]]:
    """Return names of currently active Kubernetes namespaces, or None if unavailable.

    Returns None (rather than an empty set) when the Kubernetes API cannot be reached
    so that the calling code skips filtering instead of discarding all metrics.
    The ApiClient is closed after each call to avoid aiohttp session leaks.
    """
    try:
        from kubernetes_asyncio.client import ApiClient, CoreV1Api

        from greenkube.core.k8s_client import ensure_k8s_config

        if not await ensure_k8s_config():
            return None

        async with ApiClient() as api_client:
            v1 = CoreV1Api(api_client=api_client)
            ns_list = await v1.list_namespace()
            return {ns.metadata.name for ns in ns_list.items if ns.metadata.name}
    except Exception as e:
        logger.warning("Could not list Kubernetes namespaces: %s. Skipping namespace filter.", e)
        return None


class ContextBuilder:
    """Loads metrics and cluster side inputs into an :class:`OptimizationContext`."""

    def __init__(self, config: Optional["Config"] = None):
        from greenkube.core.config import get_config

        self.config = config if config is not None else get_config()

    async def build(
        self,
        combined_repo: "CombinedMetricsRepository",
        node_repo: "NodeRepository",
        namespace: Optional[str] = None,
    ) -> OptimizationContext:
        """Reads the configured lookback window and builds a full context."""
        cfg = self.config
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=cfg.RECOMMENDATION_LOOKBACK_DAYS)
        analysis_window_seconds = (end - start).total_seconds()

        metrics = await combined_repo.read_combined_metrics_smart(start_time=start, end_time=end, namespace=namespace)

        # Remove metrics from Kubernetes namespaces that no longer exist so that
        # the engine does not regenerate recommendations for deleted namespaces,
        # allowing reconciliation to mark those rows as stale.
        active_namespaces = await get_active_k8s_namespaces()
        if active_namespaces is not None and metrics:
            metrics = [m for m in metrics if m.namespace in active_namespaces]

        # Defense in depth: repositories filter by namespace already, but a
        # custom backend may not, so enforce it here too.
        if namespace and metrics:
            metrics = [m for m in metrics if m.namespace == namespace]

        if not metrics:
            return OptimizationContext(
                config=cfg,
                metrics=[],
                namespace=namespace,
                analysis_window_seconds=analysis_window_seconds,
                window_start=start,
                window_end=end,
            )

        side_inputs = await self._collect_side_inputs(node_repo, end)
        return OptimizationContext(
            config=cfg,
            metrics=metrics,
            namespace=namespace,
            analysis_window_seconds=analysis_window_seconds,
            window_start=start,
            window_end=end,
            **side_inputs,
        )

    async def build_from_metrics(
        self,
        metrics: List[CombinedMetric],
        node_repo: "NodeRepository",
        analysis_window_seconds: Optional[float] = None,
        namespace: Optional[str] = None,
    ) -> OptimizationContext:
        """Builds a context from already-collected metrics (CLI ``--live`` path)."""
        cfg = self.config
        end = datetime.now(timezone.utc)

        if namespace:
            metrics = [m for m in metrics if m.namespace == namespace]

        if not metrics:
            return OptimizationContext(
                config=cfg,
                metrics=[],
                namespace=namespace,
                analysis_window_seconds=analysis_window_seconds,
                window_end=end,
            )

        side_inputs = await self._collect_side_inputs(node_repo, end)
        return OptimizationContext(
            config=cfg,
            metrics=metrics,
            namespace=namespace,
            analysis_window_seconds=analysis_window_seconds,
            window_end=end,
            **side_inputs,
        )

    async def _collect_side_inputs(self, node_repo: "NodeRepository", end: datetime) -> dict:
        """Collects node snapshots, HPA targets and orphaned PV/LB descriptors.

        Every collector failure is logged and degrades gracefully so that a
        missing dependency never blocks recommendation generation.
        """
        node_infos: List = []
        try:
            node_infos = await node_repo.get_latest_snapshots_before(end)
        except Exception as e:
            logger.warning("Could not fetch node snapshots for recommendations: %s", e)

        hpa_targets = None
        try:
            hpa_collector = HPACollector()
            hpa_targets = await hpa_collector.collect()
        except Exception as e:
            logger.warning("Could not collect HPA targets: %s. Proceeding without HPA filtering.", e)

        orphaned_volumes = None
        try:
            pv_collector = PVCollector()
            orphaned_volumes = await pv_collector.collect()
        except Exception as e:
            logger.warning(
                "Could not collect orphaned PersistentVolumes: %s. Proceeding without PV cleanup recommendations.",
                e,
            )

        if orphaned_volumes:
            try:
                orphaned_volumes = await enrich_orphaned_pv_costs(
                    orphaned_volumes, window_days=self.config.RECOMMENDATION_LOOKBACK_DAYS
                )
            except Exception as e:
                logger.warning(
                    "Could not enrich orphaned PV costs from OpenCost: %s. Using capacity-based estimates.",
                    e,
                )

        orphaned_load_balancers = None
        try:
            lb_collector = LoadBalancerCollector()
            orphaned_load_balancers = await lb_collector.collect()
        except Exception as e:
            logger.warning(
                "Could not collect orphaned LoadBalancer Services: %s. Proceeding without LB cleanup recommendations.",
                e,
            )

        if orphaned_load_balancers:
            try:
                orphaned_load_balancers = await enrich_orphaned_lb_costs(
                    orphaned_load_balancers, window_days=self.config.RECOMMENDATION_LOOKBACK_DAYS
                )
            except Exception as e:
                logger.warning(
                    "Could not enrich orphaned LoadBalancer costs from OpenCost: %s. Using flat estimates.",
                    e,
                )

        return {
            "node_infos": node_infos,
            "hpa_targets": hpa_targets,
            "persistent_volumes": orphaned_volumes,
            "load_balancers": orphaned_load_balancers,
        }
