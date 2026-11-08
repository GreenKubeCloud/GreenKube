# src/greenkube/models/measurement.py
"""Immutable savings measurements (C0.13, interim shape of the C1/C6 ``savings_measurements`` table).

A measurement is the decided outcome of one fixed-horizon verification of an
applied recommendation. It is written once and never updated or deleted:
re-measurement (``POST /recommendations/{id}/remeasure``) creates a *new* row
that supersedes the previous one.
"""

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from greenkube.models.verification import HealthGateResult

#: Version of the interim request-rightsizing measurement method (C0).
MEASUREMENT_STRATEGY_KEY = "legacy_before_after"
MEASUREMENT_STRATEGY_VERSION = "c0.1"


class MeasurementOutcome(str, Enum):
    """Decision recorded by a measurement (target vocabulary of C2)."""

    REALIZED = "realized"
    NOT_REALIZED = "not_realized"
    INCONCLUSIVE = "inconclusive"
    ROLLBACK_REVIEW = "rollback_review"


class QuantityOutcome(str, Enum):
    """Outcome of one measured quantity."""

    REALIZED = "realized"
    NOT_REALIZED = "not_realized"
    INCONCLUSIVE = "inconclusive"
    NOT_CLAIMED = "not_claimed"


class QuantityEstimate(BaseModel):
    """Before/after rates of one quantity and the saving claimed from them.

    Rates are per hour. ``delta_per_hour`` and ``annualized`` are signed (a
    positive value is a saving); ``claimed_annualized`` is what the
    recommendation is credited with (0 when the quantity is not claimed or not
    realized).
    """

    quantity: str
    unit: str
    basis: str
    share: float = 1.0
    before_per_hour: Optional[float] = None
    after_per_hour: Optional[float] = None
    delta_per_hour: Optional[float] = None
    annualized: Optional[float] = None
    claimed_annualized: float = 0.0
    before_coverage_steps: float = 0.0
    after_coverage_steps: float = 0.0
    outcome: QuantityOutcome
    projected_annual: Optional[float] = None
    projection_accuracy: Optional[float] = None


class SavingsMeasurement(BaseModel):
    """One immutable, decided measurement of an applied recommendation."""

    measurement_id: str
    recommendation_id: int
    group_id: str
    group_recommendation_ids: List[int] = Field(default_factory=list)
    plan_id: Optional[str] = None
    change_id: Optional[str] = None
    sequence: int = 1
    strategy_key: str = MEASUREMENT_STRATEGY_KEY
    strategy_version: str = MEASUREMENT_STRATEGY_VERSION
    impact_model_version: Optional[str] = None
    baseline_snapshot_id: Optional[str] = None
    scope: str = "workload"
    windows: dict[str, Any] = Field(default_factory=dict)
    estimates: List[QuantityEstimate] = Field(default_factory=list)
    health: List[HealthGateResult] = Field(default_factory=list)
    confounders: List[dict[str, Any]] = Field(default_factory=list)
    ledger_input: dict[str, Any] = Field(default_factory=dict)
    outcome: MeasurementOutcome
    reasons: List[str] = Field(default_factory=list)
    evidence_sha256: str = ""
    supersedes_measurement_id: Optional[str] = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "verifier"

    def with_evidence_digest(self) -> "SavingsMeasurement":
        """Return a copy whose ``evidence_sha256`` covers every decided field."""
        payload = self.model_dump(mode="json", exclude={"evidence_sha256"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return self.model_copy(update={"evidence_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()})
