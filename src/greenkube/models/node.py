# src/greenkube/models/node.py

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class NodeInfo(BaseModel):
    """
    Pydantic model for comprehensive node information from Kubernetes clusters.

    Attributes:
        name: Node name
        instance_type: Instance type (e.g., 'm5.large', 'b3-8', 'Standard_D2ps_v6')
        zone: Availability zone
        region: Cloud region
        cloud_provider: Cloud provider (ovh, azure, aws, gcp, or unknown)
        architecture: CPU architecture (amd64, arm64, etc.)
        node_pool: Node pool/agent pool name (cloud-specific)
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    name: str = Field(..., description="Node name")
    instance_type: Optional[str] = Field(None, description="Instance type")
    zone: Optional[str] = Field(None, description="Availability zone")
    region: Optional[str] = Field(None, description="Cloud region")
    cloud_provider: str = Field(default="unknown", description="Cloud provider")
    architecture: Optional[str] = Field(None, description="CPU architecture")
    node_pool: Optional[str] = Field(None, description="Node pool name")
