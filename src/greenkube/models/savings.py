# src/greenkube/models/savings.py
"""Pydantic DTOs for the recommendation savings ledger."""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class SavingsLedgerRecord(BaseModel):
    """A single prorated savings record for one collection period.

    One record is written per applied recommendation per collection cycle.
    The ``co2e_saved_grams`` and ``cost_saved_dollars`` values represent
    the fraction of the annual saving that is attributed to this period:

        co2e_period = annual_co2e / 8760 h × (period_seconds / 3600)

    Accumulating these records over time gives a proper time-series that
    Prometheus can expose as a DB-backed cumulative gauge, letting Grafana
    use ``increase(metric[$__range])`` to display savings for any window.
    """

    recommendation_id: int
    cluster_name: str = ""
    namespace: str = ""
    recommendation_type: str
    co2e_saved_grams: float = Field(default=0.0, ge=0.0)
    cost_saved_dollars: float = Field(default=0.0, ge=0.0)
    period_seconds: int = Field(default=300, gt=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 5: measurement provenance
    measurement_method: str = Field("prorated", description="'prorated' (estimate) or 'measured' (verified).")
    baseline_value: Optional[float] = Field(None, description="Baseline rate before the change.")
    actual_value: Optional[float] = Field(None, description="Measured rate after the change.")
    confidence: Optional[float] = Field(None, description="Confidence of the measurement, 0.0–1.0.")
    superseded: bool = Field(False, description="True when a later outcome invalidated this attribution.")


class SavingsCumulativeTotals(BaseModel):
    """Aggregated cumulative savings by recommendation type for a cluster."""

    cluster_name: str
    recommendation_type: str
    co2e_saved_grams: float = 0.0
    cost_saved_dollars: float = 0.0
    record_count: int = 0
