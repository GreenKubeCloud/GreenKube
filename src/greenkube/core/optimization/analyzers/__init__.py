# src/greenkube/core/optimization/analyzers/__init__.py
"""Recommendation analyzers, one per optimization domain."""

from greenkube.core.optimization.analyzers.autoscaling import AutoscalingAnalyzer
from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.analyzers.carbon_aware import CarbonAwareAnalyzer
from greenkube.core.optimization.analyzers.idle_namespace import IdleNamespaceAnalyzer
from greenkube.core.optimization.analyzers.nodes import NodesAnalyzer
from greenkube.core.optimization.analyzers.off_peak import OffPeakAnalyzer
from greenkube.core.optimization.analyzers.orphaned_lb import OrphanedLBAnalyzer
from greenkube.core.optimization.analyzers.orphaned_pv import OrphanedPVAnalyzer
from greenkube.core.optimization.analyzers.rightsizing_cpu import CpuRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing_memory import MemoryRightsizingAnalyzer
from greenkube.core.optimization.analyzers.zombie import ZombieAnalyzer

DEFAULT_ANALYZERS = (
    ZombieAnalyzer,
    CpuRightsizingAnalyzer,
    MemoryRightsizingAnalyzer,
    AutoscalingAnalyzer,
    OffPeakAnalyzer,
    IdleNamespaceAnalyzer,
    CarbonAwareAnalyzer,
    NodesAnalyzer,
    OrphanedPVAnalyzer,
    OrphanedLBAnalyzer,
)

__all__ = [
    "Analyzer",
    "DEFAULT_ANALYZERS",
    "AutoscalingAnalyzer",
    "CarbonAwareAnalyzer",
    "CpuRightsizingAnalyzer",
    "IdleNamespaceAnalyzer",
    "MemoryRightsizingAnalyzer",
    "NodesAnalyzer",
    "OffPeakAnalyzer",
    "OrphanedLBAnalyzer",
    "OrphanedPVAnalyzer",
    "ZombieAnalyzer",
]
