# src/greenkube/collectors/base_collector.py
"""
This module defines the abstract base class for all data collectors.
Enforcing this interface ensures that all collectors have a consistent
method signature, making them interchangeable and easy to manage by the
core processing logic.
"""
from abc import ABC, abstractmethod
from typing import List, Any

class BaseCollector(ABC):
    """
    Abstract Base Class for all metric collectors.
    """
    @abstractmethod
    def collect(self) -> List[Any]:
        """
        The main method for a collector. It should fetch data from its
        source (e.g., an API, a file), parse it, and return a list of
        Pydantic models.
        """
        pass

