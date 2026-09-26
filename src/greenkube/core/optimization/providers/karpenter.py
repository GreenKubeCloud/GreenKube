# src/greenkube/core/optimization/providers/karpenter.py
"""Karpenter recommendation source (stub).

Phase 6 will read Karpenter NodePool/NodeClaim state and disruption metrics to
produce node consolidation recommendations. The connector is registered now so
that enabling it later only requires implementing ``collect``.
"""

import logging
from typing import TYPE_CHECKING, List

from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.models.metrics import Recommendation

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)


class KarpenterSource(RecommendationSource):
    """Placeholder source for Karpenter node pool optimization."""

    name = "karpenter"
    priority = 2

    def __init__(self, config: "Config"):
        self.config = config

    async def is_available(self) -> bool:
        # Phase 6: detect the nodepools.karpenter.sh CRD before enabling.
        return True

    async def collect(self, context: "OptimizationContext") -> List[Recommendation]:
        logger.debug("Karpenter source is not implemented yet; returning no recommendations.")
        return []
