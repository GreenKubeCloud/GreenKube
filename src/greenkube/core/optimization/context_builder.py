# src/greenkube/core/optimization/context_builder.py
"""Builds an OptimizationContext from repositories and cluster collectors.

This module centralizes the data loading that used to be duplicated across the
API router, the startup scan and the CLI command.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, List, Optional, cast

from greenkube.collectors.hpa_collector import HPACollector
from greenkube.collectors.lb_collector import LoadBalancerCollector, enrich_orphaned_lb_costs
from greenkube.collectors.pv_collector import PVCollector, enrich_orphaned_pv_costs
from greenkube.core.optimization.context import OptimizationContext
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.base_repository import ContainerMetricsRepository

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository

logger = logging.getLogger(__name__)


class RecommendationScanLimitError(RuntimeError):
    """The configured metric-row bound cannot be met even at the minimum window."""


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
        start, expected_rows = await self._bounded_window(combined_repo, end, namespace=namespace)
        analysis_window_seconds = (end - start).total_seconds()

        metrics = await combined_repo.read_combined_metrics_smart(start_time=start, end_time=end, namespace=namespace)
        if isinstance(combined_repo, ContainerMetricsRepository):
            container_metrics = await combined_repo.read_container_metrics_smart(
                start_time=start,
                end_time=end,
                namespace=namespace,
            )
        else:
            logger.warning(
                "Combined-metrics backend does not support container metrics; "
                "container-targeted recommendations will be unavailable."
            )
            container_metrics = []

        if expected_rows is not None:
            logger.debug(
                "Recommendation scan window is %.0f hours with %d bounded metric rows.",
                analysis_window_seconds / 3600,
                expected_rows,
            )

        # Remove metrics from Kubernetes namespaces that no longer exist so that
        # the engine does not regenerate recommendations for deleted namespaces,
        # allowing reconciliation to mark those rows as stale.
        active_namespaces = await get_active_k8s_namespaces()
        if active_namespaces is not None and metrics:
            metrics = [m for m in metrics if m.namespace in active_namespaces]
        if active_namespaces is not None and container_metrics:
            container_metrics = [m for m in container_metrics if m.namespace in active_namespaces]

        # Defense in depth: repositories filter by namespace already, but a
        # custom backend may not, so enforce it here too.
        if namespace and metrics:
            metrics = [m for m in metrics if m.namespace == namespace]
        if namespace and container_metrics:
            container_metrics = [m for m in container_metrics if m.namespace == namespace]

        # Collect side inputs even when there are no pod metrics: orphaned
        # PV/LB recommendations do not depend on pod activity.
        side_inputs = await self._collect_side_inputs(node_repo, end)

        if not metrics and not container_metrics:
            return OptimizationContext(
                config=cfg,
                metrics=[],
                container_metrics=[],
                namespace=namespace,
                analysis_window_seconds=analysis_window_seconds,
                window_start=start,
                window_end=end,
                **side_inputs,
            )

        return OptimizationContext(
            config=cfg,
            metrics=metrics,
            container_metrics=container_metrics,
            namespace=namespace,
            analysis_window_seconds=analysis_window_seconds,
            window_start=start,
            window_end=end,
            **side_inputs,
        )

    async def _bounded_window(
        self,
        combined_repo: "CombinedMetricsRepository",
        end: datetime,
        *,
        namespace: Optional[str],
    ) -> tuple[datetime, Optional[int]]:
        """Shrink the lookback window before loading rows that exceed the configured bound."""
        cfg = self.config
        budget = int(cfg.RECOMMENDATION_MAX_METRIC_ROWS)
        window = timedelta(days=cfg.RECOMMENDATION_LOOKBACK_DAYS)
        minimum = timedelta(hours=1)
        last_total: Optional[int] = None
        while True:
            start = end - window
            combined_count = await self._count_rows(
                combined_repo,
                "count_combined_metrics_smart",
                start,
                end,
                namespace,
            )
            container_count = await self._count_rows(
                combined_repo,
                "count_container_metrics_smart",
                start,
                end,
                namespace,
            )
            if combined_count is None or container_count is None:
                return start, None
            last_total = combined_count + container_count
            if last_total <= budget:
                if window < timedelta(days=cfg.RECOMMENDATION_LOOKBACK_DAYS):
                    logger.warning(
                        "Recommendation lookback shortened to %.0f hours to stay below %d metric rows.",
                        window.total_seconds() / 3600,
                        budget,
                    )
                return start, last_total
            if window <= minimum:
                raise RecommendationScanLimitError(
                    f"recommendation scan requires {last_total} metric rows, above the configured "
                    f"RECOMMENDATION_MAX_METRIC_ROWS bound of {budget}"
                )
            window = max(minimum, timedelta(seconds=window.total_seconds() / 2))

    async def _count_rows(
        self,
        repository: object,
        method_name: str,
        start: datetime,
        end: datetime,
        namespace: Optional[str],
    ) -> Optional[int]:
        method = getattr(repository, method_name, None)
        if not callable(method):
            return None
        try:
            value = await cast(Any, method)(start_time=start, end_time=end, namespace=namespace)
        except Exception as exc:
            logger.debug("Metric row count is unavailable: %s", exc)
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

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
        container_metrics = [metric for metric in metrics if metric.container_name]
        metrics = [metric for metric in metrics if not metric.container_name]

        # Collect side inputs even when there are no pod metrics: orphaned
        # PV/LB recommendations do not depend on pod activity.
        side_inputs = await self._collect_side_inputs(node_repo, end)

        if not metrics and not container_metrics:
            return OptimizationContext(
                config=cfg,
                metrics=[],
                container_metrics=[],
                namespace=namespace,
                analysis_window_seconds=analysis_window_seconds,
                window_end=end,
                **side_inputs,
            )

        return OptimizationContext(
            config=cfg,
            metrics=metrics,
            container_metrics=container_metrics,
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
