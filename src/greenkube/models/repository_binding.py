"""Repository bindings discovered for Kubernetes workloads."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BindingSource(StrEnum):
    """Source which supplied a repository binding."""

    ARGOCD = "argocd"
    FLUX = "flux"
    ANNOTATION = "annotation"


class BindingStatus(StrEnum):
    """Resolution status for a workload binding."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class RepositoryBinding(BaseModel):
    """A persisted association between a workload and its Git repository."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    cluster: str = ""
    namespace: str
    workload_kind: str
    workload_name: str
    repo_url: str
    path: str | None = None
    branch: str | None = None
    source: BindingSource
    priority: int = Field(ge=0)
    status: BindingStatus = BindingStatus.RESOLVED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_status(self) -> "RepositoryBinding":
        if self.status == BindingStatus.AMBIGUOUS and self.confidence >= 1:
            self.confidence = 0.0
        return self

    @property
    def workload_key(self) -> tuple[str, str, str]:
        return self.namespace, self.workload_kind, self.workload_name


class RepositoryBindingQuery(BaseModel):
    """Identity used to retrieve bindings for a workload."""

    model_config = ConfigDict(extra="forbid")

    cluster: str = ""
    namespace: str
    workload_kind: str
    workload_name: str


class BindingResolution(BaseModel):
    """Result of deterministic priority and ambiguity resolution."""

    status: BindingStatus
    binding: RepositoryBinding | None = None
    candidates: list[RepositoryBinding] = Field(default_factory=list)
    reason: str | None = None
