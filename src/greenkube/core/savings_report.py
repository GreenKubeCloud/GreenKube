# src/greenkube/core/savings_report.py
"""Assemble the savings report served by ``GET /recommendations/savings``.

Projected, measured and user-declared savings are reported in separate blocks
and are never summed (product principle 3, roadmap C0.14):

- annual blocks are the *current* yearly rates of applied recommendations;
- ``*_to_date`` blocks are the restated totals of the hash-chained savings
  ledger v2 over the window (all time without a window), each restricted to
  one method (LED-04): after a measurement the measured period is moved from
  ``projected`` to ``measured``. The accrued book alone stays visible
  (``accrued_only_*``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Iterable, List, Optional

from ..models.metrics import RecommendationRecord
from ..models.savings import (
    METHOD_MEASURED,
    METHOD_PROJECTED,
    METHOD_USER_DECLARED,
    RealizationSummary,
    SavingsAccrualTotal,
    SavingsBlock,
    SavingsReport,
)
from .savings_attributor import InvalidSavingsRecord, accrual_components, annual_rates_by_method

LEGACY_COMPRESSED_DIAGNOSTIC = (
    "legacy_compressed: part of this window comes from hourly ledger rows compressed before 0.4; "
    "those totals may be understated (BUG-32)."
)


def _status(record: RecommendationRecord) -> str:
    status = record.status
    return str(status.value if hasattr(status, "value") else status)


def _annual_block(rates: dict, method: str) -> SavingsBlock:
    values = rates.get(method, {})
    return SavingsBlock(
        method=method,  # type: ignore[arg-type]
        basis="annual_rate",
        co2e_grams=float(values.get("co2e_grams", 0.0)),
        cost=float(values.get("cost", 0.0)),
        recommendation_count=int(values.get("count", 0)),
    )


def _realization_factor(record: RecommendationRecord) -> Optional[float]:
    impact = record.impact if isinstance(record.impact, dict) else {}
    value = impact.get("realization_factor")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return min(max(float(value), 0.0), 1.0)


def realization_summary(records: Iterable[RecommendationRecord]) -> RealizationSummary:
    """Measured allocated rates of ``records`` and the billed saving their realization factors imply.

    Also the projected allocated rate of the changes still accruing as
    projected and its billable expectation (allocated x realization factor):
    a derived figure, never accrued (LED-01).
    """
    records = list(records)
    cost = co2e = billed_cost = billed_co2e = weighted = 0.0
    count = without = 0
    for record in records:
        try:
            components = [item for item in accrual_components(record) if item.method == METHOD_MEASURED]
        except (InvalidSavingsRecord, ValueError):
            continue
        if not components:
            continue
        count += 1
        measured_cost = math.fsum(item.cost_per_year for item in components)
        measured_co2e = math.fsum(item.co2e_grams_per_year for item in components)
        cost += measured_cost
        co2e += measured_co2e
        factor = _realization_factor(record)
        if factor is None:
            without += 1
            continue
        billed_cost += measured_cost * factor
        billed_co2e += measured_co2e * factor
        weighted += measured_cost
    known = count - without
    projected_cost = 0.0
    projected_billable: Optional[float] = None
    for record in records:
        try:
            components = [item for item in accrual_components(record) if item.method == METHOD_PROJECTED]
        except (InvalidSavingsRecord, ValueError):
            continue
        rate = math.fsum(item.cost_per_year for item in components)
        if not components:
            continue
        projected_cost += rate
        factor = _realization_factor(record)
        if factor is not None:
            projected_billable = (projected_billable or 0.0) + rate * factor
    return RealizationSummary(
        projected_allocated_cost_annual=projected_cost,
        projected_billable_cost_annual_expected=projected_billable,
        measured_allocated_cost_annual=cost,
        measured_allocated_co2e_grams_annual=co2e,
        realization_factor=(billed_cost / weighted) if weighted else None,
        realized_billed_cost_annual_expected=billed_cost if known else None,
        realized_billed_co2e_grams_annual_expected=billed_co2e if known else None,
        recommendation_count=count,
        without_realization_count=without,
    )


LEDGER_V1_FALLBACK_DIAGNOSTIC = (
    "ledger_v1_fallback: the savings ledger v2 has no cluster identity yet; the to-date figures come from the "
    "v1 staging ledger and do not include restatements."
)


def _accrued_block(
    breakdown: Iterable[SavingsAccrualTotal],
    method: str,
    start: Optional[datetime],
    end: Optional[datetime],
    *,
    source: Optional[str] = None,
    accrued_only: Optional[Iterable[SavingsAccrualTotal]] = None,
) -> SavingsBlock:
    rows = [row for row in breakdown if row.method == method]
    only = [row for row in accrued_only if row.method == method] if accrued_only is not None else None
    return SavingsBlock(
        method=method,  # type: ignore[arg-type]
        basis="accrued",
        co2e_grams=math.fsum(row.co2e_saved_grams for row in rows),
        cost=math.fsum(row.cost_saved_dollars for row in rows),
        start=start,
        end=end,
        source=source,  # type: ignore[arg-type]
        accrued_only_co2e_grams=math.fsum(row.co2e_saved_grams for row in only) if only is not None else None,
        accrued_only_cost=math.fsum(row.cost_saved_dollars for row in only) if only is not None else None,
    )


def assemble_savings_report(
    applied_records: List[RecommendationRecord],
    breakdown: List[SavingsAccrualTotal],
    *,
    namespace: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    legacy_compressed: bool = False,
    accrued_only: Optional[List[SavingsAccrualTotal]] = None,
    source: str = "ledger_v2",
    projected_unverified: Optional[List[SavingsAccrualTotal]] = None,
    cost_of_fix: Optional[List[SavingsAccrualTotal]] = None,
) -> SavingsReport:
    """Build the report from applied recommendations and ledger totals.

    ``breakdown`` is the restated ledger v2 (``LedgerRepository.accrual_breakdown``,
    both books, LED-04) and ``accrued_only`` its accrued book alone; with
    ``source="ledger_v1"`` it is the v1 staging ledger (fallback, flagged).
    ``projected_unverified`` holds the ledger totals of the changes whose
    measurement was inconclusive: reported apart and left out of
    ``projected_to_date`` (LED-03). ``cost_of_fix`` holds the measured cost of
    reliability fixes, reported in ``reliability_cost_to_date``, never netted
    into a saving.
    """
    rates = annual_rates_by_method(applied_records)
    blocks = {"source": source, "accrued_only": accrued_only}
    realized = _accrued_block(breakdown, METHOD_MEASURED, start, end, **blocks)  # type: ignore[arg-type]
    projected_to_date = _accrued_block(breakdown, METHOD_PROJECTED, start, end, **blocks)  # type: ignore[arg-type]
    unverified_block: Optional[SavingsBlock] = None
    if projected_unverified is not None:
        # LED-03: the projections of inconclusive changes are kept but never shown as projected savings to date.
        unverified_block = _accrued_block(projected_unverified, METHOD_PROJECTED, start, end, source=source)
        projected_to_date = projected_to_date.model_copy(
            update={
                "cost": projected_to_date.cost - unverified_block.cost,
                "co2e_grams": projected_to_date.co2e_grams - unverified_block.co2e_grams,
            }
        )
    diagnostics = [LEGACY_COMPRESSED_DIAGNOSTIC] if legacy_compressed else []
    if source == "ledger_v1":
        diagnostics.append(LEDGER_V1_FALLBACK_DIAGNOSTIC)

    counts: dict[str, int] = defaultdict(int)
    for record in applied_records:
        counts[record.namespace or "_cluster"] += 1
    realized_by_namespace: dict[str, dict[str, float]] = defaultdict(lambda: {"co2e": 0.0, "cost": 0.0})
    for row in breakdown:
        if row.method == METHOD_MEASURED:
            bucket = realized_by_namespace[row.namespace or "_cluster"]
            bucket["co2e"] += row.co2e_saved_grams
            bucket["cost"] += row.cost_saved_dollars
    namespace_breakdown = [
        {
            "namespace": ns,
            "carbon_saved_co2e_grams": realized_by_namespace[ns]["co2e"],
            "cost_saved": realized_by_namespace[ns]["cost"],
            "measured_carbon_saved_co2e_grams": realized_by_namespace[ns]["co2e"],
            "measured_cost_saved": realized_by_namespace[ns]["cost"],
            "count": counts.get(ns, 0),
        }
        for ns in sorted(set(counts) | set(realized_by_namespace))
    ]

    return SavingsReport(
        namespace=namespace,
        window_start=start,
        window_end=end,
        projected_annual=_annual_block(rates, METHOD_PROJECTED),
        run_rate_annual=_annual_block(rates, METHOD_MEASURED),
        realized_to_date=realized,
        projected_to_date=projected_to_date,
        user_declared_annual=_annual_block(rates, METHOD_USER_DECLARED),
        projected_unverified_to_date=unverified_block,
        reliability_cost_to_date=(
            _accrued_block(cost_of_fix, METHOD_MEASURED, start, end, source=source) if cost_of_fix is not None else None
        ),
        user_declared_to_date=_accrued_block(
            breakdown,
            METHOD_USER_DECLARED,
            start,
            end,
            **blocks,  # type: ignore[arg-type]
        ),
        applied_count=len(applied_records),
        verified_count=sum(1 for record in applied_records if _status(record) == "verified"),
        diagnostics=diagnostics,
        realization=realization_summary(applied_records),
        total_carbon_saved_co2e_grams=realized.co2e_grams,
        total_cost_saved=realized.cost,
        measured_carbon_saved_co2e_grams=realized.co2e_grams,
        measured_cost_saved=realized.cost,
        prorated_carbon_saved_co2e_grams=projected_to_date.co2e_grams,
        prorated_cost_saved=projected_to_date.cost,
        namespace_breakdown=namespace_breakdown,
    )
