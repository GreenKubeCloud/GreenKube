# src/greenkube/models/region_mapping.py

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RegionMapping(BaseModel):
    """
    Pydantic model for cloud region to Electricity Maps zone mapping.

    Attributes:
        cloud_provider: Name of the cloud provider (e.g., "Google Cloud Platform", "Amazon Web Services")
        region_id: The cloud provider's region identifier (e.g., "us-east-1", "europe-west9")
        electricity_maps_zone: The corresponding Electricity Maps zone code (e.g., "US-MIDA-PJM", "FR")
        location_description: Optional human-readable description of the location
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    cloud_provider: str = Field(..., description="Cloud provider name")
    region_id: str = Field(..., description="Cloud region identifier")
    electricity_maps_zone: str = Field(..., description="Electricity Maps zone code")
    location_description: Optional[str] = Field(None, description="Location description")
