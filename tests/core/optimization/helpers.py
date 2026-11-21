# tests/core/optimization/helpers.py
"""Synchronous test helper around the native recommendation source.

Production code generates recommendations through
:class:`~greenkube.core.optimization.engine.OptimizationEngine`. The analyzer
behavior suite is synchronous, so this helper runs the native source and
finalization steps in a private event loop.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Set, Tuple

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.dedup import finalize_recommendations
from greenkube.core.optimization.providers.native import NativeSource
from greenkube.models.metrics import CombinedMetric, Recommendation


def _run_coroutine(coroutine):
    """Runs a coroutine from synchronous code, even inside a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    # An event loop is already running (async test): use a worker thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


class NativeRecommender:
    """Runs the built-in analyzers synchronously for tests."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.min_cpu_millicores = self.config.RECOMMENDATION_MIN_CPU_MILLICORES
        self.min_memory_bytes = self.config.RECOMMENDATION_MIN_MEMORY_BYTES
        self.recommend_system_namespaces = self.config.RECOMMEND_SYSTEM_NAMESPACES

    def generate_recommendations(
        self,
        metrics: List[CombinedMetric],
        node_infos: Optional[List] = None,
        hpa_targets: Optional[Set[Tuple[str, str, str]]] = None,
        persistent_volumes: Optional[List] = None,
        load_balancers: Optional[List] = None,
        analysis_window_seconds: Optional[float] = None,
    ) -> List[Recommendation]:
        if not metrics:
            return []

        context = OptimizationContext(
            config=self.config,
            metrics=metrics,
            node_infos=node_infos or [],
            hpa_targets=hpa_targets,
            persistent_volumes=persistent_volumes,
            load_balancers=load_balancers,
            analysis_window_seconds=analysis_window_seconds,
        )
        recommendations = _run_coroutine(NativeSource(self.config).collect(context))
        return finalize_recommendations(recommendations, self.config)
