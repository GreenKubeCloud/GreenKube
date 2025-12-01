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
        cpu_capacity_cores: CPU capacity in cores
        memory_capacity_bytes: Memory capacity in bytes
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    name: str = Field(..., description="Node name")
    instance_type: Optional[str] = Field(None, description="Instance type")
    zone: Optional[str] = Field(None, description="Availability zone")
    region: Optional[str] = Field(None, description="Cloud region")
    cloud_provider: str = Field(default="unknown", description="Cloud provider")
    architecture: Optional[str] = Field(None, description="CPU architecture")
    node_pool: Optional[str] = Field(None, description="Node pool name")
    cpu_capacity_cores: Optional[float] = Field(None, description="CPU capacity in cores")
    memory_capacity_bytes: Optional[int] = Field(None, description="Memory capacity in bytes")
