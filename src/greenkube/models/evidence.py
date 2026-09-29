# src/greenkube/models/evidence.py
"""Review-grade evidence attached to every recommendation.

The evidence block is the artifact a reviewer reads: observation window,
current vs proposed resources, utilization distribution, expected savings,
risk, expiry and rollback conditions. It must be self-sufficient — reviewing a
recommendation should not require re-running the analysis.
"""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


class UtilizationStats(BaseModel):
    """Distribution of an observed usage signal over the analysis window."""

    avg: float = 0.0
    p50: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    max: float = 0.0
    sample_count: int = 0
    coverage_ratio: float = 1.0


class ResourceSnapshot(BaseModel):
    """Resource requests and limits at a point in time."""

    cpu_request_millicores: Optional[int] = None
    cpu_limit_millicores: Optional[int] = None
    memory_request_bytes: Optional[int] = None
    memory_limit_bytes: Optional[int] = None


class ProposedChange(BaseModel):
    """A single proposed resource change."""

    resource: Literal["cpu", "memory", "node", "storage", "network"]
    current: Optional[float] = None
    proposed: Optional[float] = None
    change_ratio: Optional[float] = None


class RollbackCondition(BaseModel):
    """A condition that should trigger review or rollback after apply."""

    metric: str
    comparator: Literal["gt", "gte", "lt", "lte", "outside_range"]
    threshold: float
    window_seconds: int = 3600
    action: Literal["review", "revert"] = "review"
    description: str = ""


class PatchV2(BaseModel):
    """Typed, versioned action plan retained in review evidence."""

    version: int = 2
    operations: List[dict[str, Any]] = Field(default_factory=list)
    container_name: Optional[str] = None
    manifest_path: Optional[str] = None
    kind: Optional[str] = None
    namespace: Optional[str] = None
    name: Optional[str] = None

    def __getitem__(self, key: str) -> Any:
        """Preserve mapping-style access for legacy evidence consumers."""
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        """Return a field using the legacy mapping-style contract."""
        return getattr(self, key, default)


class BaselinePayload(BaseModel):
    """Structured pre-apply baseline included in lifecycle event payloads."""

    captured_at: Optional[datetime] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    sample_count: int = 0


class EventPayload(BaseModel):
    """Versioned lifecycle event context."""

    version: int = 2
    baseline: Optional[BaselinePayload] = None
    details: dict[str, Any] = Field(default_factory=dict)


class RecommendationEvidence(BaseModel):
    """Structured justification block persisted with each recommendation."""

    observation_window_start: Optional[datetime] = None
    observation_window_end: Optional[datetime] = None
    observation_window_seconds: Optional[float] = None
    sample_count: int = 0
    coverage_ratio: float = 1.0
    current: ResourceSnapshot = Field(default_factory=ResourceSnapshot)
    proposed: ResourceSnapshot = Field(default_factory=ResourceSnapshot)
    changes: List[ProposedChange] = Field(default_factory=list)
    cpu_usage: Optional[UtilizationStats] = None
    memory_usage: Optional[UtilizationStats] = None
    restart_count: Optional[int] = None
    oom_events: Optional[int] = None
    cost_per_hour_before: Optional[float] = None
    co2e_grams_per_hour_before: Optional[float] = None
    proposed_patch: Optional[PatchV2] = None
    proposed_diff: Optional[str] = None
    expected_savings_cost_annual: Optional[float] = None
    expected_savings_co2e_grams_annual: Optional[float] = None
    savings_method: str = "unknown"
    confidence: float = 0.0
    confidence_factors: dict = Field(default_factory=dict)
    risk_level: str = "medium"
    risk_factors: List[str] = Field(default_factory=list)
    rollback_conditions: List[RollbackCondition] = Field(default_factory=list)
    expires_at: Optional[datetime] = None
    generated_by: str = "greenkube"
