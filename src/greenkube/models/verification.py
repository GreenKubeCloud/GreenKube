"""Typed inputs produced by the Phase 5 verification pipeline."""

from datetime import datetime, timezone
from typing import Optional

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
    """Auditable, measured values used when attributing verified savings."""

    before_cost_per_hour: Optional[float] = None
    after_cost_per_hour: Optional[float] = None
    before_co2e_grams_per_hour: Optional[float] = None
    after_co2e_grams_per_hour: Optional[float] = None
    sample_count: int = 0
    readiness_ratio: Optional[float] = None
    traffic_ratio: Optional[float] = None
    seasonality_factor: float = 1.0
