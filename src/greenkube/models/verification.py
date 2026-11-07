"""Typed inputs produced by the Phase 5 verification pipeline."""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class KubernetesHealthObservation(BaseModel):
    """Health signals observed for a workload during a verification window."""

    namespace: Optional[str] = None
    workload: Optional[str] = None
    ready_pods: int = 0
    total_pods: int = 0
    restart_count: int = 0
    oom_kill_count: int = 0
    unavailable_pods: int = 0
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def readiness_ratio(self) -> Optional[float]:
        if self.total_pods <= 0:
            return None
        return self.ready_pods / self.total_pods


class WorkloadHealthObservation(BaseModel):
    """Health signals observed over a verification window (C0.9).

    ``None`` means the signal was not available (no kube-state-metrics, no CPU
    limit, Prometheus unreachable...). It is reported as ``not_available`` by
    the matching health gate, never silently treated as a pass.
    """

    oom_kill_count: Optional[int] = None
    readiness_ratio: Optional[float] = None
    throttling_ratio: Optional[float] = None
    source: str = "prometheus"
    pod_count: int = 0
    notes: List[str] = Field(default_factory=list)


class HealthGateStatus(str, Enum):
    """Result of one verification health gate."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_AVAILABLE = "not_available"


class HealthGateResult(BaseModel):
    """One evaluated health gate, persisted with the measurement and the event payload."""

    gate: str
    status: HealthGateStatus
    value: Optional[float] = None
    threshold: Optional[float] = None
    detail: str = ""


class TrafficSeasonalityControl(BaseModel):
    """Factors used to make before/after measurements comparable."""

    before_traffic: Optional[float] = None
    after_traffic: Optional[float] = None
    seasonality_factor: float = Field(1.0, gt=0)

    @property
    def traffic_ratio(self) -> Optional[float]:
        if self.before_traffic is None or self.before_traffic <= 0 or self.after_traffic is None:
            return None
        return self.after_traffic / self.before_traffic


class MeasuredLedgerInput(BaseModel):
    """Auditable, measured values used when attributing verified savings.

    ``traffic_ratio`` is informational only: traffic normalization is disabled
    until the load-driver counterfactual (C3) ships (BUG-27). ``before_*`` and
    ``after_*`` rates are the claimed basis of the recommendation (its cost
    component or its share of an attribution group), not necessarily the whole
    workload.
    """

    before_cost_per_hour: Optional[float] = None
    after_cost_per_hour: Optional[float] = None
    before_co2e_grams_per_hour: Optional[float] = None
    after_co2e_grams_per_hour: Optional[float] = None
    sample_count: int = 0
    readiness_ratio: Optional[float] = None
    traffic_ratio: Optional[float] = None
    seasonality_factor: float = 1.0
    measurement_id: Optional[str] = None
    group_id: Optional[str] = None
    outcome: Optional[str] = None
    cost_outcome: Optional[str] = None
    co2e_outcome: Optional[str] = None
    cost_basis: Optional[str] = None
    measurement_scope: Optional[str] = None
    baseline_window_start: Optional[datetime] = None
    baseline_window_end: Optional[datetime] = None
    after_window_start: Optional[datetime] = None
    after_window_end: Optional[datetime] = None


class RemeasureRequest(BaseModel):
    """Body of ``POST /recommendations/{id}/remeasure`` (C0.5)."""

    reason: Optional[str] = Field(None, max_length=500, description="Why a new measurement is requested.")


class DismissRollbackReviewRequest(BaseModel):
    """Body of ``POST /recommendations/{id}/rollback-review/dismiss`` (C0.11)."""

    reason: str = Field(..., min_length=1, max_length=500, description="Why the health finding is a false positive.")
