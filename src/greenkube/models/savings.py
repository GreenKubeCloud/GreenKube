# src/greenkube/models/savings.py
"""Pydantic DTOs for the recommendation savings ledger.

The ledger stores *accruals*: the share of an annual savings rate that was
earned during one slot of a fixed UTC grid. Every accrual carries the method
that produced it, and methods are never summed together (product principle 3):

- ``projected``: GreenKube's projection for an applied change that has not been
  measured yet (formerly labelled ``prorated``);
- ``measured``: the verifier's measured savings for a verified change;
- ``user_declared``: values supplied by a user on ``PATCH /apply``; they are
  validated but never presented as measured.
"""

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

SavingsMethod = Literal["projected", "measured", "user_declared"]

METHOD_PROJECTED = "projected"
METHOD_MEASURED = "measured"
#: ``row_kind`` of the reversal row the drift monitor writes (LED-12).
DRIFT_REVERSAL_ROW_KIND = "drift_reversal"
METHOD_USER_DECLARED = "user_declared"
SAVINGS_METHODS: tuple[str, ...] = (METHOD_PROJECTED, METHOD_MEASURED, METHOD_USER_DECLARED)

# Accrual slots are one UTC hour long and aligned on the hour.
ACCRUAL_SLOT_SECONDS = 3600


def normalize_savings_method(value: Any) -> str:
    """Map legacy method labels onto the current vocabulary.

    ``prorated`` rows written before 0.4 are projections and are reported as
    ``projected``. Unknown values are returned unchanged so they stay visible.
    """
    if value is None or value == "" or value == "prorated":
        return METHOD_PROJECTED
    return str(value)


def floor_to_slot(value: datetime, slot_seconds: int = ACCRUAL_SLOT_SECONDS) -> datetime:
    """Return the start of the UTC grid slot that contains ``value``."""
    aware = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    epoch = int(aware.timestamp())
    return datetime.fromtimestamp(epoch - epoch % slot_seconds, tz=timezone.utc)


def _is_grid_slot(start: datetime, end: datetime) -> bool:
    return (end - start).total_seconds() == ACCRUAL_SLOT_SECONDS and floor_to_slot(start) == start


class SavingsLedgerRecord(BaseModel):
    """One accrual of savings for one recommendation and one period.

    ``co2e_saved_grams`` and ``cost_saved_dollars`` hold the fraction of the
    annual rate earned during ``[period_start, period_end)``:

        accrual = annual_rate × (period_end − period_start) / seconds_per_year

    ``slot_start`` is the start of the fixed UTC grid slot the accrual belongs
    to. Together with ``recommendation_id`` and ``measurement_method`` it is the
    idempotency key of the ledger, so the same slot can never be accrued twice.
    Values are signed: a negative user-declared saving is recorded as such.
    """

    recommendation_id: int
    cluster_name: str = ""
    namespace: str = ""
    recommendation_type: str
    co2e_saved_grams: float = 0.0
    cost_saved_dollars: float = 0.0
    period_seconds: int = Field(default=300, gt=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    period_start: datetime | None = None
    period_end: datetime | None = None
    slot_start: datetime | None = Field(
        None, description="Start of the fixed UTC accrual slot (idempotency key with recommendation and method)."
    )
    measurement_method: str = Field(
        METHOD_PROJECTED, description="'projected', 'measured' or 'user_declared'. Never summed together."
    )
    baseline_value: Optional[float] = Field(None, description="Annual projected CO2e saving of the recommendation.")
    actual_value: Optional[float] = Field(None, description="Annual CO2e rate used for this accrual.")
    confidence: Optional[float] = Field(None, description="Confidence attached to the source rate, 0.0–1.0.")
    superseded: bool = Field(False, description="True when a later outcome invalidated this attribution.")
    basis: Optional[str] = Field(
        None,
        description=(
            "Accounting basis of the amounts (LED-01): allocated, billable_expected, billed or declared. "
            "None only for rows written without one (drift reversals and demo rows: inferred from the method)."
        ),
    )
    annual_cost_rate: Optional[float] = Field(
        None, description="Annual cost rate the row was accrued from (LED-11); None on rows stored before 0293."
    )
    annual_co2e_rate: Optional[float] = Field(
        None, description="Annual CO2e rate (grams) the row was accrued from (LED-11); None before 0293."
    )
    currency: Optional[str] = Field(
        None, description="Currency of cost_saved_dollars (the projection's currency, LED-11); None before 0293."
    )
    measurement_id: Optional[str] = Field(
        None, description="Measurement whose rate a measured row uses, read at accrual time (LED-11)."
    )
    row_kind: Optional[str] = Field(
        None,
        description=(
            "What the row is when it is not an accrual (LED-12): 'drift_reversal' for the reversal the drift "
            "monitor writes. None for accruals and for rows stored before migration 0291."
        ),
    )

    @field_validator("co2e_saved_grams", "cost_saved_dollars")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("savings accruals must be finite numbers")
        return value

    @field_validator("measurement_method", mode="before")
    @classmethod
    def _method(cls, value: Any) -> str:
        return normalize_savings_method(value)

    def model_post_init(self, __context: object) -> None:
        """Keep legacy timestamp-only records representable as explicit periods."""
        if self.period_end is None:
            self.period_end = self.timestamp
        if self.period_start is None:
            self.period_start = self.period_end - timedelta(seconds=self.period_seconds)
        if self.slot_start is None and _is_grid_slot(self.period_start, self.period_end):
            # A record that covers exactly one grid slot is that slot.
            self.slot_start = self.period_start


class SavingsCumulativeTotals(BaseModel):
    """Aggregated cumulative savings by recommendation type for a cluster."""

    cluster_name: str
    recommendation_type: str
    co2e_saved_grams: float = 0.0
    cost_saved_dollars: float = 0.0
    record_count: int = 0


class SavingsAccrualTotal(BaseModel):
    """Net ledger accruals for one (namespace, recommendation type, method) group."""

    namespace: str = ""
    recommendation_type: str = "unknown"
    method: str = METHOD_PROJECTED
    co2e_saved_grams: float = 0.0
    cost_saved_dollars: float = 0.0


class SavingsCounterTotal(BaseModel):
    """Monotonic lifetime counters for one (recommendation type, method) group.

    ``accrued_*`` only ever grows as accruals are appended. ``reversed_*`` grows
    when accruals are superseded (rollback review) or when a negative accrual is
    recorded. The net saving is ``accrued − reversed``.
    """

    recommendation_type: str = "unknown"
    method: str = METHOD_PROJECTED
    accrued_co2e_grams: float = 0.0
    accrued_cost_dollars: float = 0.0
    reversed_co2e_grams: float = 0.0
    reversed_cost_dollars: float = 0.0


class UserDeclaredSavings(BaseModel):
    """Annual savings a user declared on ``PATCH /apply``, after validation.

    Stored on the recommendation's baseline under ``user_declared_savings``;
    the attributor accrues declared quantities with ``method=user_declared``.
    """

    co2e_grams_per_year: Optional[float] = None
    cost_per_year: Optional[float] = None
    projected_co2e_grams_per_year: Optional[float] = None
    projected_cost_per_year: Optional[float] = None
    flags: List[str] = Field(default_factory=list, description="Validation flags, e.g. negative values.")
    declared_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SavingsBlock(BaseModel):
    """One savings figure with its method. Blocks are never summed together."""

    method: SavingsMethod
    basis: Literal["annual_rate", "accrued"] = Field(
        ..., description="'annual_rate' for current yearly rates, 'accrued' for ledger sums over the window."
    )
    co2e_grams: float = 0.0
    cost: float = 0.0
    value_basis: Optional[Literal["allocated", "billable_expected", "declared", "mixed"]] = Field(
        None,
        description=(
            "Basis of the values of an annual block (IMP-06): 'allocated' (Impact Model allocated projection, the "
            "basis measurements are compared with), 'billable_expected' (legacy records without an Impact Model "
            "projection), 'declared', or 'mixed'. Null when no record contributes or for accrued blocks."
        ),
    )
    recommendation_count: int = Field(0, description="Recommendations contributing to an annual rate.")
    start: Optional[datetime] = Field(None, description="Window start for accrued blocks (None means all time).")
    end: Optional[datetime] = Field(None, description="Window end for accrued blocks.")
    source: Optional[Literal["ledger_v2", "ledger_v1"]] = Field(
        None,
        description=(
            "For accrued blocks: 'ledger_v2' (restated totals of the hash-chained ledger, LED-04) or "
            "'ledger_v1' (fallback before the ledger has a cluster identity, flagged in diagnostics)."
        ),
    )
    accrued_only_co2e_grams: Optional[float] = Field(
        None, description="Accrued book alone (without restatements), for transparency during the transition."
    )
    accrued_only_cost: Optional[float] = Field(
        None, description="Accrued book alone (without restatements), for transparency during the transition."
    )


class RealizationSummary(BaseModel):
    """How the measured run rate relates to the bill (projected != allocated != realized).

    GreenKube measures *allocated* savings (``measured_allocated``: what the
    changed workloads stop being charged). The bill drops only when capacity
    is removed; the realization factor of each recommendation's projection is
    the expected share. ``realized_billed_*_expected`` are expectations, not
    measurements (``realized_billed_measured`` stays false until billed
    savings are measured, roadmap C10).
    """

    measured_allocated_cost_annual: float = Field(0.0, description="Same as run_rate_annual.cost.")
    measured_allocated_co2e_grams_annual: float = Field(0.0, description="Same as run_rate_annual.co2e_grams.")
    realization_factor: Optional[float] = Field(
        None, description="Measured-cost-weighted realization factor of the measured recommendations' projections."
    )
    realized_billed_cost_annual_expected: Optional[float] = Field(
        None, description="Sum of measured allocated cost x realization factor (an expectation, not a measurement)."
    )
    realized_billed_co2e_grams_annual_expected: Optional[float] = Field(
        None, description="Sum of measured allocated CO2e x realization factor (an expectation)."
    )
    recommendation_count: int = Field(0, description="Recommendations with a measured annual rate.")
    without_realization_count: int = Field(
        0, description="Measured recommendations whose projection has no realization factor (left out of the factor)."
    )
    realized_billed_measured: bool = Field(False, description="Whether billed savings are measured (not yet).")
    projected_allocated_cost_annual: float = Field(
        0.0,
        description=(
            "Projected annual cost saving of the changes still accruing as projected, on the allocated basis "
            "(the basis of the projected accruals since ledger/2, same as projected_annual.cost)."
        ),
    )
    projected_billable_cost_annual_expected: Optional[float] = Field(
        None,
        description=(
            "Projected allocated cost x realization factor: the share expected on the bill. A derived "
            "expectation, never accrued in the ledger (None when no projection has a realization factor)."
        ),
    )


def _deprecated(description: str) -> Any:
    return Field(0.0, description=f"Deprecated: {description}", json_schema_extra={"deprecated": True})


class SavingsReport(BaseModel):
    """Response of ``GET /recommendations/savings``.

    Three never-mixed blocks answer the core questions:

    - ``projected_annual``: yearly savings GreenKube projects for applied
      changes that have not been measured yet;
    - ``run_rate_annual``: yearly savings measured on verified changes;
    - ``realized_to_date``: measured savings accrued in the ledger over the
      window (all time when no window is requested).

    ``projected_to_date`` and the ``user_declared_*`` blocks are reported
    separately and are never part of a realized figure. The flat ``total_*``,
    ``measured_*`` and ``prorated_*`` fields are deprecated aliases kept for the
    0.3 UI: ``total_*`` and ``measured_*`` equal ``realized_to_date`` and
    ``prorated_*`` equals ``projected_to_date``.
    """

    namespace: Optional[str] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    projected_annual: SavingsBlock
    run_rate_annual: SavingsBlock
    realized_to_date: SavingsBlock
    projected_to_date: SavingsBlock
    user_declared_annual: SavingsBlock
    user_declared_to_date: SavingsBlock
    reliability_cost_to_date: Optional[SavingsBlock] = Field(
        None,
        description=(
            "Measured cost of reliability fixes (request increases) over the window: negative, a cost and never "
            "a saving, kept out of realized_to_date (ledger reason cost_of_fix)."
        ),
    )
    projected_unverified_to_date: Optional[SavingsBlock] = Field(
        None,
        description=(
            "Projected accruals of changes whose measurement was inconclusive (LED-03): kept, never confirmed, "
            "and left out of projected_to_date. Projections refuted by a not-realized verdict are reversed."
        ),
    )
    interim_run_rate_annual: Optional[SavingsBlock] = Field(
        None,
        description=(
            "Yearly savings of recommendations decided by the interim verifier (CHANGE_MEASUREMENT_ENABLED=false: "
            "two window means, no counterfactual, no confounder check, MEA-16). Never part of run_rate_annual; "
            "null when there is none."
        ),
    )
    interim_realized_to_date: Optional[SavingsBlock] = Field(
        None,
        description=(
            "Measured ledger accruals of the interim-method recommendations over the window (MEA-16), left out "
            "of realized_to_date."
        ),
    )
    opportunity: Optional["OpportunitySummary"] = Field(
        None,
        description=(
            "Projected opportunity of the active recommendations, de-duplicated across overlapping "
            "recommendations (IMP-07): the one figure the dashboard and the recommendations page show. Null when "
            "the active recommendations could not be read."
        ),
    )
    applied_count: int = Field(0, description="Recommendations in an applied, verifying, verified or review state.")
    verified_count: int = Field(0, description="Recommendations with a verified (measured) outcome.")
    diagnostics: List[str] = Field(default_factory=list, description="Caveats that apply to these figures.")
    measured_basis: Literal["measured_allocated"] = Field(
        "measured_allocated",
        description="run_rate_annual and realized_to_date are measured allocated savings (ledger v2 method "
        "measured_allocated), not billed savings.",
    )
    realization: RealizationSummary = Field(
        default_factory=RealizationSummary,
        description="Measured allocated run rate, its realization factor and the billed saving it implies.",
    )

    total_carbon_saved_co2e_grams: float = _deprecated("same as realized_to_date.co2e_grams.")
    total_cost_saved: float = _deprecated("same as realized_to_date.cost.")
    measured_carbon_saved_co2e_grams: float = _deprecated("same as realized_to_date.co2e_grams.")
    measured_cost_saved: float = _deprecated("same as realized_to_date.cost.")
    prorated_carbon_saved_co2e_grams: float = _deprecated("same as projected_to_date.co2e_grams.")
    prorated_cost_saved: float = _deprecated("same as projected_to_date.cost.")
    namespace_breakdown: List[dict] = Field(
        default_factory=list,
        description="Deprecated: realized accruals per namespace.",
        json_schema_extra={"deprecated": True},
    )


from greenkube.core.opportunity import OpportunitySummary  # noqa: E402  (after SavingsReport: no import cycle)

SavingsReport.model_rebuild()
