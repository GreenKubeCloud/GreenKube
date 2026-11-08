# src/greenkube/core/savings_report.py
"""Assemble the savings report served by ``GET /recommendations/savings``.

Projected, measured and user-declared savings are reported in separate blocks
and are never summed (product principle 3, roadmap C0.14):

- annual blocks are the *current* yearly rates of applied recommendations;
- ``*_to_date`` blocks are ledger accruals over the window (all time without
  a window), each restricted to one method.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, List, Optional

from ..models.metrics import RecommendationRecord
from ..models.savings import (
    METHOD_MEASURED,
    METHOD_PROJECTED,
    METHOD_USER_DECLARED,
    SavingsAccrualTotal,
    SavingsBlock,
    SavingsReport,
)
from .savings_attributor import annual_rates_by_method

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


def _accrued_block(
    breakdown: Iterable[SavingsAccrualTotal],
    method: str,
    start: Optional[datetime],
    end: Optional[datetime],
) -> SavingsBlock:
    rows = [row for row in breakdown if row.method == method]
    return SavingsBlock(
        method=method,  # type: ignore[arg-type]
        basis="accrued",
        co2e_grams=sum(row.co2e_saved_grams for row in rows),
        cost=sum(row.cost_saved_dollars for row in rows),
        start=start,
        end=end,
    )


def assemble_savings_report(
    applied_records: List[RecommendationRecord],
    breakdown: List[SavingsAccrualTotal],
    *,
    namespace: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    legacy_compressed: bool = False,
) -> SavingsReport:
    """Build the report from applied recommendations and ledger accruals."""
    rates = annual_rates_by_method(applied_records)
    realized = _accrued_block(breakdown, METHOD_MEASURED, start, end)
    projected_to_date = _accrued_block(breakdown, METHOD_PROJECTED, start, end)

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
        user_declared_to_date=_accrued_block(breakdown, METHOD_USER_DECLARED, start, end),
        applied_count=len(applied_records),
        verified_count=sum(1 for record in applied_records if _status(record) == "verified"),
        diagnostics=[LEGACY_COMPRESSED_DIAGNOSTIC] if legacy_compressed else [],
        total_carbon_saved_co2e_grams=realized.co2e_grams,
        total_cost_saved=realized.cost,
        measured_carbon_saved_co2e_grams=realized.co2e_grams,
        measured_cost_saved=realized.cost,
        prorated_carbon_saved_co2e_grams=projected_to_date.co2e_grams,
        prorated_cost_saved=projected_to_date.cost,
        namespace_breakdown=namespace_breakdown,
    )
