# src/greenkube/collectors/base_electricity_provider.py
"""
Abstract base class for grid electricity (carbon intensity) data providers.

Providers expose a uniform ``collect()`` contract so that the data pipelines
remain agnostic to the upstream data source (Electricity Maps, Wattnet, ...).
Each implementation must return a list of records shaped like the Electricity
Maps history payload so it can be persisted via
:class:`~greenkube.storage.base_repository.CarbonIntensityRepository.save_history`:

.. code-block:: python

    {
        "carbonIntensity": float,   # gCO2e/kWh
        "datetime": str,            # ISO 8601 UTC timestamp
        "zone": str,                # zone code used by GreenKube (Electricity Maps code)
        "isEstimated": bool,
        "estimationMethod": str,
    }
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime

logger = logging.getLogger(__name__)


class BaseElectricityProvider(ABC):
    """
    Abstract base class for grid carbon-intensity providers.

    Implementations are responsible for authentication, fetching carbon
    intensity time series for a given zone, and translating provider-specific
    data quality flags into the ``isEstimated`` / ``estimationMethod`` fields
    understood by the rest of GreenKube.
    """

    @abstractmethod
    async def collect(self, zone: str, target_datetime: datetime | None = None) -> list:
        """
        Retrieve carbon intensity history for a zone and return it as a list
        of Electricity-Maps-shaped records (see module docstring).

        Args:
            zone: The GreenKube zone code (Electricity Maps zone naming).
            target_datetime: Optional timestamp the caller needs data around.
                If omitted, implementations should return the most recent data
                (e.g. the last 24 hours).

        Returns:
            A list of dicts with the keys described in the module docstring.
            An empty list means no data could be retrieved.
        """
        pass

    async def close(self):
        """
        Release any resources held by the provider (HTTP clients, tokens, ...).
        """
        pass
