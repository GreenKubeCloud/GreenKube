# src/greenkube/reporters/base_reporter.py
"""
Defines the abstract base class for all reporters.
"""
from abc import ABC, abstractmethod
from typing import List
from ..models.metrics import CombinedMetric

class BaseReporter(ABC):
    """
    Abstract Base Class for all reporters.
    """
    @abstractmethod
    def report(self, data: List[CombinedMetric], group_by: str = "namespace", sort_by: str = "cost", recommendations=None):
        """
        Takes the final processed data and presents it in a specific format
        (e.g., console, CSV, JSON).
        """
        pass

