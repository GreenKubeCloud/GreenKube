# src/greenkube/core/optimization/analyzers/base.py
"""Base contract for recommendation analyzers."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List

from greenkube.models.metrics import Recommendation

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext


class Analyzer(ABC):
    """Analyzes an optimization context and produces recommendations.

    Analyzers are stateless beyond their configuration and must not perform
    I/O: everything they need is available on the context.
    """

    #: Optimization domain, used for cross-source arbitration (Phase 1).
    capability: str = ""

    def __init__(self, config: "Config"):
        self.config = config

    @abstractmethod
    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        """Returns the recommendations identified in the given context."""
        raise NotImplementedError

    def scan_diagnostics(self) -> List[Dict[str, Any]]:
        """Notes about the last :meth:`analyze` call (``level``, ``code``, ``message``, ``count``).

        The source adapter records them on the source run so the API and UI can
        show why advice was withheld. None by default.
        """
        return []

    def scan_complete(self) -> bool:
        """False when the last :meth:`analyze` call could not judge every target.

        An incomplete result keeps the previous advice (it is never marked stale
        by this scan). True by default.
        """
        return True
