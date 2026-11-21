# src/greenkube/verify/ledger/model.py
"""Entries of the append-only savings ledger v2 and their hash chain (roadmap C6).

An entry is one signed amount of one quantity (``cost`` or ``co2e_g``) earned
during ``[period_start, period_end)`` under one method. Entries are never
updated or deleted (database triggers reject it): a correction is a new entry.

* ``accrual`` — an amount earned (positive or negative: values are signed and
  never floored);
* ``reversal`` — cancels the entry named by ``reverses_entry_id`` (or, for a
  drift reversal, the measured accruals of its period);
* ``restatement`` — replaces a projected accrual (``restates_entry_id``) with
  the measured amount of the same period; it always comes with the
  ``reversal`` of that projected accrual (BUG-39).

``book`` keeps the two views apart: ``accrued`` entries mirror the v1 ledger
(``recommendation_savings_ledger``, a staging table since LED-04);
``restatement`` entries (restatements, the
reversals of the projections they restate, and later reversals of either)
move projected amounts to measured ones. The restated total of a method is
the sum over both books: what ``GET /recommendations/savings``, the savings
counters and the dashboard gauges show.

Each cluster's entries form a hash chain: ``seq`` is a gap-free position and
``hash = sha256(prev_hash || canonical_json(entry))``, where the canonical
JSON covers every column except ``hash`` and ``prev_hash``.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from greenkube.utils.time import ensure_utc

ENTRY_ACCRUAL = "accrual"
ENTRY_REVERSAL = "reversal"
ENTRY_RESTATEMENT = "restatement"
ENTRY_KINDS = (ENTRY_ACCRUAL, ENTRY_REVERSAL, ENTRY_RESTATEMENT)

BOOK_ACCRUED = "accrued"
BOOK_RESTATEMENT = "restatement"
BOOKS = (BOOK_ACCRUED, BOOK_RESTATEMENT)

METHOD_PROJECTED = "projected"
METHOD_MEASURED_ALLOCATED = "measured_allocated"
METHOD_REALIZED_BILLED = "realized_billed"
METHOD_USER_DECLARED = "user_declared"
LEDGER_METHODS = (METHOD_PROJECTED, METHOD_MEASURED_ALLOCATED, METHOD_REALIZED_BILLED, METHOD_USER_DECLARED)

#: Why an entry was written.
REASON_ACCRUAL = "accrual"
REASON_DRIFT = "drift"
REASON_SUPERSESSION = "supersession"
REASON_RESTATEMENT = "restatement"
#: A projection reversed because its measurement refuted it (not realized, LED-03).
REASON_NOT_REALIZED = "not_realized"
#: A supersession reversal cancelled because the rollback review was dismissed (LED-08).
REASON_REINSTATEMENT = "reinstatement"
#: The measured cost of a reliability fix (a request increase), booked as a negative cost, never a saving.
REASON_COST_OF_FIX = "cost_of_fix"
#: Released capacity accrued from the change measurement.
REASON_RELEASED_CAPACITY = "released_capacity"
REASONS = (
    REASON_COST_OF_FIX,
    REASON_RELEASED_CAPACITY,
    REASON_ACCRUAL,
    REASON_DRIFT,
    REASON_SUPERSESSION,
    REASON_RESTATEMENT,
    REASON_NOT_REALIZED,
    REASON_REINSTATEMENT,
)

#: Accounting basis of an amount (LED-01): what the number measures. One basis per method.
BASIS_ALLOCATED = "allocated"
BASIS_BILLABLE_EXPECTED = "billable_expected"
BASIS_BILLED = "billed"
BASIS_DECLARED = "declared"
BASES = (BASIS_ALLOCATED, BASIS_BILLABLE_EXPECTED, BASIS_BILLED, BASIS_DECLARED)
#: Basis of an entry stored before the ``basis`` column existed (migration 0160): projections were
#: accrued on the expected-billable basis (allocated x realization factor) until ledger/2.
LEGACY_BASIS = {
    METHOD_PROJECTED: BASIS_BILLABLE_EXPECTED,
    METHOD_MEASURED_ALLOCATED: BASIS_ALLOCATED,
    METHOD_REALIZED_BILLED: BASIS_BILLED,
    METHOD_USER_DECLARED: BASIS_DECLARED,
}

QUANTITY_COST = "cost"
QUANTITY_CO2E = "co2e_g"
#: Released capacity (WP-08.1): reserved CPU and memory released by a change, never money.
QUANTITY_CPU_CORE_HOURS = "cpu_core_h"
QUANTITY_MEM_GIB_HOURS = "mem_gib_h"
CAPACITY_QUANTITIES = (QUANTITY_CPU_CORE_HOURS, QUANTITY_MEM_GIB_HOURS)
#: Money and carbon: the quantities of savings totals (capacity is reported apart, never added).
SAVINGS_QUANTITIES = (QUANTITY_COST, QUANTITY_CO2E)
QUANTITIES = (QUANTITY_COST, QUANTITY_CO2E, QUANTITY_CPU_CORE_HOURS, QUANTITY_MEM_GIB_HOURS)
CO2E_UNIT = "gCO2e"
CAPACITY_UNITS = {QUANTITY_CPU_CORE_HOURS: "core-hours", QUANTITY_MEM_GIB_HOURS: "GiB-hours"}

#: v1 ledger method labels mapped onto the v2 vocabulary.
V1_METHODS = {
    "projected": METHOD_PROJECTED,
    "prorated": METHOD_PROJECTED,
    "": METHOD_PROJECTED,
    "measured": METHOD_MEASURED_ALLOCATED,
    "user_declared": METHOD_USER_DECLARED,
}

#: Version of the ledger writer; part of every entry's ``method_version`` for v1 projections.
#: ``ledger/2`` entries carry their ``basis`` (part of the hash payload); ``ledger/1`` entries have none.
#: ``ledger/3`` (IMP-06) scales the allocated projection of a rightsizing applied more conservatively than
#: recommended by the share of the cut it realizes.
LEDGER_WRITER_VERSION = "ledger/3"

_NAMESPACE = uuid.UUID("6f1d3c52-5a0e-4c39-9a51-3b8ad0c4f6e1")


def v2_method(v1_method: Optional[str]) -> str:
    """The v2 method of a v1 ledger row (``measured`` rows measured allocated cost)."""
    return V1_METHODS.get(v1_method or "", str(v1_method))


def basis_of(method: str, stored: Optional[str] = None) -> str:
    """The accounting basis of an entry: the stored one, else the basis its method had before ledger/2."""
    if stored:
        return stored
    return LEGACY_BASIS.get(method, BASIS_ALLOCATED)


def entry_id_for(cluster_uid: str, entry_key: str) -> str:
    """Deterministic entry id: the same entry key always gets the same id."""
    return uuid.uuid5(_NAMESPACE, f"{cluster_uid}|{entry_key}").hex


def format_time(value: Optional[datetime]) -> Optional[str]:
    """Canonical timestamp: UTC, microseconds, ``Z`` suffix (also the SQLite storage format)."""
    if value is None:
        return None
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


@dataclass(frozen=True)
class LedgerEntryDraft:
    """An entry before it is placed on the chain (no seq, no hash)."""

    entry_key: str
    group_key: str
    entry_kind: str
    book: str
    reason: str
    method: str
    quantity: str
    value: float
    unit: str
    method_version: str
    period_start: datetime
    period_end: datetime
    created_by: str
    currency: Optional[str] = None
    recommendation_id: Optional[int] = None
    recommendation_ids: List[int] = field(default_factory=list)
    namespace: str = ""
    recommendation_type: str = ""
    change_id: Optional[str] = None
    measurement_id: Optional[str] = None
    pr_url: Optional[str] = None
    price_card_version: Optional[str] = None
    intensity_g_per_kwh: Optional[float] = None
    intensity_source: Optional[str] = None
    confidence: Optional[float] = None
    slot_start: Optional[datetime] = None
    reverses_entry_id: Optional[str] = None
    restates_entry_id: Optional[str] = None
    source_table: Optional[str] = None
    source_id: Optional[int] = None
    #: Accounting basis (LED-01); ``None`` only on entries stored before migration 0160.
    basis: Optional[str] = None

    def __post_init__(self) -> None:
        if self.entry_kind not in ENTRY_KINDS:
            raise ValueError(f"unknown entry kind {self.entry_kind!r}")
        if self.book not in BOOKS:
            raise ValueError(f"unknown book {self.book!r}")
        if self.method not in LEDGER_METHODS:
            raise ValueError(f"unknown ledger method {self.method!r}")
        if self.basis is not None and self.basis not in BASES:
            raise ValueError(f"unknown basis {self.basis!r}")
        if not math.isfinite(float(self.value)):
            raise ValueError("ledger values must be finite")
        if ensure_utc(self.period_end) < ensure_utc(self.period_start):
            raise ValueError("period_end precedes period_start")


@dataclass(frozen=True)
class LedgerEntry(LedgerEntryDraft):
    """An entry on the chain."""

    entry_id: str = ""
    cluster_uid: str = ""
    cluster_name: str = ""
    seq: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    prev_hash: Optional[str] = None
    hash: str = ""

    def canonical(self) -> str:
        """Canonical JSON of every column except ``hash`` and ``prev_hash``."""
        return canonical_json(self.payload())

    def payload(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("hash", None)
        data.pop("prev_hash", None)
        if data.get("basis") is None:
            # Entries stored before the basis column keep the payload (and hash) they were chained with.
            data.pop("basis", None)
        for key in ("period_start", "period_end", "slot_start", "created_at"):
            data[key] = format_time(data[key])
        data["value"] = float(self.value)
        data["recommendation_ids"] = sorted(int(item) for item in self.recommendation_ids)
        for key in ("confidence", "intensity_g_per_kwh"):
            if data[key] is not None:
                data[key] = float(data[key])
        return data

    @property
    def effective_basis(self) -> str:
        """The stored basis, or the one inferred from the method for entries stored before it existed."""
        return basis_of(self.method, self.basis)

    def computed_hash(self) -> str:
        return chain_hash(self.prev_hash, self.canonical())

    def as_dict(self) -> Dict[str, Any]:
        data = self.payload()
        data["prev_hash"] = self.prev_hash
        data["hash"] = self.hash
        data["basis"] = self.effective_basis
        data["basis_stored"] = self.basis is not None
        return data


def canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def chain_hash(prev_hash: Optional[str], canonical: str) -> str:
    return hashlib.sha256(((prev_hash or "") + canonical).encode("utf-8")).hexdigest()


def place(
    draft: LedgerEntryDraft,
    *,
    cluster_uid: str,
    cluster_name: str,
    seq: int,
    prev_hash: Optional[str],
    created_at: datetime,
) -> LedgerEntry:
    """Put ``draft`` on the chain after ``prev_hash`` at position ``seq``."""
    values = {name: getattr(draft, name) for name in LedgerEntryDraft.__dataclass_fields__}
    entry = LedgerEntry(
        **values,
        entry_id=entry_id_for(cluster_uid, draft.entry_key),
        cluster_uid=cluster_uid,
        cluster_name=cluster_name,
        seq=seq,
        created_at=ensure_utc(created_at),
        prev_hash=prev_hash,
    )
    return replace(entry, hash=entry.computed_hash())


__all__ = [
    "BASES",
    "BASIS_ALLOCATED",
    "BASIS_BILLABLE_EXPECTED",
    "BASIS_BILLED",
    "BASIS_DECLARED",
    "LEGACY_BASIS",
    "basis_of",
    "BOOKS",
    "BOOK_ACCRUED",
    "BOOK_RESTATEMENT",
    "CO2E_UNIT",
    "ENTRY_ACCRUAL",
    "ENTRY_KINDS",
    "ENTRY_RESTATEMENT",
    "ENTRY_REVERSAL",
    "LEDGER_METHODS",
    "LEDGER_WRITER_VERSION",
    "LedgerEntry",
    "LedgerEntryDraft",
    "METHOD_MEASURED_ALLOCATED",
    "METHOD_PROJECTED",
    "METHOD_REALIZED_BILLED",
    "METHOD_USER_DECLARED",
    "QUANTITIES",
    "QUANTITY_CO2E",
    "QUANTITY_COST",
    "REASONS",
    "REASON_ACCRUAL",
    "CAPACITY_QUANTITIES",
    "CAPACITY_UNITS",
    "QUANTITY_CPU_CORE_HOURS",
    "QUANTITY_MEM_GIB_HOURS",
    "REASON_COST_OF_FIX",
    "REASON_RELEASED_CAPACITY",
    "SAVINGS_QUANTITIES",
    "REASON_DRIFT",
    "REASON_NOT_REALIZED",
    "REASON_REINSTATEMENT",
    "REASON_RESTATEMENT",
    "REASON_SUPERSESSION",
    "canonical_json",
    "chain_hash",
    "entry_id_for",
    "format_time",
    "parse_time",
    "place",
    "v2_method",
]
