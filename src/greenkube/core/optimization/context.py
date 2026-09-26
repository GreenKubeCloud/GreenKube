# src/greenkube/core/optimization/context.py
"""Execution context passed to recommendation sources and analyzers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from greenkube.models.metrics import CombinedMetric

if TYPE_CHECKING:
    from greenkube.core.config import Config


@dataclass
class OptimizationContext:
    """All inputs required to generate recommendations for one analysis run.

    The context is built once per scan by
    :class:`~greenkube.core.optimization.context_builder.ContextBuilder` and
    shared by every recommendation source.
    """

    config: "Config"
    metrics: List[CombinedMetric] = field(default_factory=list)
    namespace: Optional[str] = None
    analysis_window_seconds: Optional[float] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    node_infos: List = field(default_factory=list)
    hpa_targets: Optional[Set[Tuple[str, str, str]]] = None
    persistent_volumes: Optional[List] = None
    load_balancers: Optional[List] = None
    _target_series_cache: Optional[Dict[Tuple[str, str, str], List[CombinedMetric]]] = field(
        default=None, init=False, repr=False, compare=False
    )

    def target_series(self) -> Dict[Tuple[str, str, str], List[CombinedMetric]]:
        """Returns metrics grouped by stable workload owner, computed once."""
        if self._target_series_cache is None:
            from greenkube.core.optimization.targeting import group_by_recommendation_target

            self._target_series_cache = group_by_recommendation_target(self.metrics)
        return self._target_series_cache
