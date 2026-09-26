# src/greenkube/core/optimization/providers/native.py
"""Native GreenKube recommendation source backed by analyzers."""

from typing import TYPE_CHECKING, List, Optional, Sequence, Type

from greenkube.core.optimization.analyzers import DEFAULT_ANALYZERS
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.models.metrics import Recommendation

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.analyzers.base import Analyzer
    from greenkube.core.optimization.context import OptimizationContext


class NativeSource(RecommendationSource):
    """Runs the built-in analyzers over the optimization context."""

    name = "greenkube"
    priority = 0

    def __init__(
        self,
        config: "Config",
        analyzer_classes: Optional[Sequence[Type["Analyzer"]]] = None,
    ):
        self.config = config
        self.analyzers: List["Analyzer"] = [cls(config) for cls in (analyzer_classes or DEFAULT_ANALYZERS)]

    async def is_available(self) -> bool:
        return True

    async def collect(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        for analyzer in self.analyzers:
            recs.extend(analyzer.analyze(context))
        return recs
