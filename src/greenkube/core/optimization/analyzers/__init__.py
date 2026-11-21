"""Recommendation analyzers, one per optimization domain."""

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.analyzers.orphaned_lb import OrphanedLBAnalyzer
from greenkube.core.optimization.analyzers.orphaned_pv import OrphanedPVAnalyzer
from greenkube.core.optimization.analyzers.rightsizing_cpu import CpuRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing_memory import MemoryRightsizingAnalyzer

DEFAULT_ANALYZERS = (
    CpuRightsizingAnalyzer,
    MemoryRightsizingAnalyzer,
    OrphanedPVAnalyzer,
    OrphanedLBAnalyzer,
)

__all__ = [
    "Analyzer",
    "DEFAULT_ANALYZERS",
    "CpuRightsizingAnalyzer",
    "MemoryRightsizingAnalyzer",
    "OrphanedLBAnalyzer",
    "OrphanedPVAnalyzer",
]
