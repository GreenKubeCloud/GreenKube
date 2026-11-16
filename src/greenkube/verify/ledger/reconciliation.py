# src/greenkube/verify/ledger/reconciliation.py
"""Reconciliation of the v1 staging ledger with the accrued book of ledger v2 (LED-04).

The v1 table ``recommendation_savings_ledger`` is a write-ahead staging area:
every user-facing savings number is read from ``savings_ledger_entries``. The
invariant checked on every ``ledger-chain`` tick is that, per method and
quantity, the net ``accrued`` book of v2 equals the net of the v1 rows that
are not superseded (within :data:`RELATIVE_TOLERANCE`). A mismatch means a
v1 row was not projected yet, or a supersession not reversed yet: it is
exported as ``greenkube_ledger_reconciliation_ok`` = 0 and alerted.
"""

from __future__ import annotations

import logging
import math

from prometheus_client import Gauge

from greenkube.core.observability import REGISTRY as OBSERVABILITY_REGISTRY

logger = logging.getLogger(__name__)

RELATIVE_TOLERANCE = 1e-9
#: Below this magnitude two totals are equal whatever their relative difference (float noise).
ABSOLUTE_TOLERANCE = 1e-9

LEDGER_RECONCILIATION_OK = Gauge(
    "greenkube_ledger_reconciliation_ok",
    "1 when the accrued book of the savings ledger v2 equals the non-superseded v1 rows per method and "
    "quantity (relative 1e-9), 0 otherwise.",
    ["cluster_uid"],
    registry=OBSERVABILITY_REGISTRY,
)


async def reconcile(repo, cluster_uid: str, cluster_name: str) -> bool:
    """Check the invariant for one cluster, set the gauge and return whether it holds."""
    v2 = await repo.accrued_book_totals(cluster_uid)
    v1 = await repo.v1_totals(cluster_name)
    mismatches = []
    for key in sorted(set(v1) | set(v2)):
        left, right = v2.get(key, 0.0), v1.get(key, 0.0)
        if not math.isclose(left, right, rel_tol=RELATIVE_TOLERANCE, abs_tol=ABSOLUTE_TOLERANCE):
            mismatches.append((key, left, right))
    ok = not mismatches
    LEDGER_RECONCILIATION_OK.labels(cluster_uid=cluster_uid).set(1.0 if ok else 0.0)
    for (method, quantity), left, right in mismatches:
        logger.warning(
            "Savings ledger v2 accrued book does not reconcile with v1 for %s %s: v2 %r, v1 %r.",
            method,
            quantity,
            left,
            right,
        )
    return ok


__all__ = ["LEDGER_RECONCILIATION_OK", "reconcile"]
