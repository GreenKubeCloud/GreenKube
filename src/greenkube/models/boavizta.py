"""Response models of the Boavizta API v1 (``/v1/cloud/instance``, ``/v1/server/``).

The v1 API answers ``{"impacts": {"gwp": {"unit": "kgCO2eq", "embedded": {"value": 24.0,
"min": ..., "max": ..., "warnings": [...]}, "use": {...}}}}``; with ``verbose=true`` a
``verbose`` object is added. Unknown fields are ignored so the model survives additive
API changes (NRG-01).
"""

from typing import Any, List, Optional, Union

from pydantic import BaseModel, ConfigDict

#: Unit of the global warming potential values GreenKube can use.
GWP_UNIT = "kgCO2eq"


class BoaviztaRequest(BaseModel):
    provider: str
    instance_type: str
    verbose: bool = False
    criteria: str = "gwp"


class BoaviztaImpactValue(BaseModel):
    """One impact figure of the v1 API: a value with its uncertainty range."""

    model_config = ConfigDict(extra="ignore")

    value: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    warnings: Optional[List[str]] = None


class BoaviztaGwp(BaseModel):
    """Global warming potential of one asset.

    ``embedded`` and ``use`` are objects in API v1; a bare number is accepted
    too. ``manufacture`` is the pre-v1 field, kept for older self-hosted APIs.
    """

    model_config = ConfigDict(extra="ignore")

    unit: Optional[str] = None
    embedded: Optional[Union[BoaviztaImpactValue, float]] = None
    use: Optional[Union[BoaviztaImpactValue, float]] = None
    manufacture: Optional[float] = None

    def embedded_kg(self) -> Optional[float]:
        """Lifetime embodied emissions in kg CO2e, or ``None`` when absent or in another unit."""
        if self.unit is not None and self.unit != GWP_UNIT:
            return None
        embedded = self.embedded
        value = embedded.value if isinstance(embedded, BoaviztaImpactValue) else embedded
        if value is None:
            value = self.manufacture
        if value is None or value <= 0:
            return None
        return float(value)


class BoaviztaImpacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gwp: Optional[BoaviztaGwp] = None


class BoaviztaResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    impacts: Optional[BoaviztaImpacts] = None
    verbose: Optional[Any] = None

    def embedded_kg(self) -> Optional[float]:
        """Lifetime embodied emissions in kg CO2e of the asset, or ``None``."""
        if self.impacts is None or self.impacts.gwp is None:
            return None
        return self.impacts.gwp.embedded_kg()
