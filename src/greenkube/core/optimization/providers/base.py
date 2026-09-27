# src/greenkube/core/optimization/providers/base.py
"""Base contract for recommendation sources (connectors)."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

from greenkube.models.metrics import Recommendation

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class RecommendationSource(ABC):
    """A source of optimization recommendations.

    Implementations are connectors (native analyzers, VPA, Karpenter, ...)
    that translate an external signal into engine recommendations. Sources
    must be resilient: raising from ``collect`` only skips that source, but
    graceful degradation with a warning is preferred.
    """

    #: Stable source identifier (e.g. ``greenkube``, ``vpa``, ``karpenter``).
    name: str = "unknown"

    #: Arbitration priority when multiple sources own the same capability.
    #: Higher wins. Phase 1 uses this for cross-source deduplication.
    priority: int = 0

    @abstractmethod
    async def is_available(self) -> bool:
        """Returns True when the source can run in the current environment."""
        raise NotImplementedError

    @abstractmethod
    async def collect(self, context: "OptimizationContext") -> List[Recommendation]:
        """Returns the recommendations observed by this source."""
        raise NotImplementedError
