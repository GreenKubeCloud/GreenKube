from typing import Optional

from pydantic import BaseModel


class BoaviztaRequest(BaseModel):
    provider: str
    instance_type: str
    verbose: bool = True
    criteria: str = "gwp"


class BoaviztaGwp(BaseModel):
    manufacture: Optional[float] = None
    use: Optional[float] = None
    unit: Optional[str] = None


class BoaviztaImpacts(BaseModel):
    gwp: Optional[BoaviztaGwp] = None


class BoaviztaResponse(BaseModel):
    impacts: Optional[BoaviztaImpacts] = None
    verbose: Optional[bool] = None
