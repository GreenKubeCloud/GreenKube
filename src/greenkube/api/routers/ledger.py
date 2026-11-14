# src/greenkube/api/routers/ledger.py
"""Savings ledger v2: entries, totals and exports (roadmap C7).

Endpoints (prefix ``/api/v2``):
  GET /ledger/entries   - Entries newest first, cursor-paginated (?cursor=&limit= and filters)
  GET /ledger/summary   - Signed totals per method and quantity, by book, and the chain status
  GET /ledger/export    - CSV (?format=csv, one row per entry) or FOCUS-aligned CSV (?format=focus)

Filters: ``namespace``, ``method``, ``quantity``, ``entry_kind``, ``book``,
``reason``, ``recommendation_id``, ``change_id``, ``measurement_id`` and a
window (``last=7d`` or both ``start`` and ``end``, ISO-8601; an entry is
included when its period overlaps the window).

The ledger is append-only and hash-chained per cluster (``cluster_uid``, the
``kube-system`` namespace UID). Methods are never summed together: every total
names its method (``projected``, ``measured_allocated``, ``realized_billed``,
``user_declared``) and quantity (``cost`` in the pricing currency, ``co2e_g``).
The ``accrued`` book reconciles with ``GET /api/v1/recommendations/savings``
(``measured_allocated`` ↔ ``realized_to_date``, ``projected`` ↔
``projected_to_date``, ``user_declared`` ↔ ``user_declared_to_date``); the
``restated`` total adds the restatement book, which replaces projected
accruals by measured ones once a change is measured (BUG-39). With a window,
the totals and the exports' ``window_share`` columns prorate entries that
overlap a window boundary, as the savings report does.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from greenkube.api.dependencies import validate_namespace
from greenkube.storage.ledger_repository import LedgerRepository, get_ledger_repository
from greenkube.utils.time import ensure_utc, parse_duration
from greenkube.verify.ledger.model import (
    BOOK_ACCRUED,
    BOOKS,
    ENTRY_KINDS,
    LEDGER_METHODS,
    QUANTITIES,
    QUANTITY_CO2E,
    QUANTITY_COST,
    REASONS,
    LedgerEntry,
    format_time,
)

logger = logging.getLogger(__name__)

#: What each ledger method measures (projected != allocated != realized), for labels.
METHOD_BASIS: Dict[str, str] = {
    "projected": "projection of an applied change that is not measured yet",
    "measured_allocated": "measured allocated saving (what the workload stops being charged); not the bill",
    "realized_billed": "measured billed saving (removed node-hours on the bill)",
    "user_declared": "value declared by a user, never measured",
}

router = APIRouter()

MAX_LIMIT = 500
EXPORT_PAGE = 5000
#: FOCUS 1.0 columns, then the GreenKube extension columns.
FOCUS_COLUMNS = (
    "BillingAccountId",
    "BillingAccountName",
    "SubAccountId",
    "BillingPeriodStart",
    "BillingPeriodEnd",
    "ChargePeriodStart",
    "ChargePeriodEnd",
    "ChargeCategory",
    "ChargeClass",
    "ChargeDescription",
    "BilledCost",
    "EffectiveCost",
    "BillingCurrency",
    "ProviderName",
    "PublisherName",
    "ServiceName",
    "ServiceCategory",
    "ResourceId",
    "ResourceType",
    "Tags",
    "x-greenkube-entry-id",
    "x-greenkube-seq",
    "x-greenkube-entry-kind",
    "x-greenkube-book",
    "x-greenkube-reason",
    "x-greenkube-method",
    "x-greenkube-method-version",
    "x-greenkube-saving",
    "x-greenkube-co2e-g",
    "x-greenkube-window-share",
    "x-greenkube-measurement-id",
    "x-greenkube-change-id",
    "x-greenkube-recommendation-ids",
    "x-greenkube-pr-url",
    "x-greenkube-reverses-entry-id",
    "x-greenkube-restates-entry-id",
    "x-greenkube-hash",
)
CSV_COLUMNS = (
    "seq",
    "entry_id",
    "cluster_uid",
    "cluster_name",
    "entry_kind",
    "book",
    "reason",
    "method",
    "quantity",
    "value",
    "unit",
    "currency",
    "window_share",
    "window_value",
    "period_start",
    "period_end",
    "slot_start",
    "namespace",
    "recommendation_type",
    "recommendation_id",
    "recommendation_ids",
    "change_id",
    "measurement_id",
    "pr_url",
    "method_version",
    "confidence",
    "price_card_version",
    "intensity_g_per_kwh",
    "intensity_source",
    "reverses_entry_id",
    "restates_entry_id",
    "source_table",
    "source_id",
    "created_at",
    "created_by",
    "prev_hash",
    "hash",
)


async def get_ledger_repo() -> LedgerRepository:
    """Provide the ledger repository for the configured backend."""
    return get_ledger_repository()


def _cluster_name() -> str:
    from greenkube.core.savings_attributor import savings_cluster_name

    return savings_cluster_name()


async def _cluster(repo: LedgerRepository) -> Tuple[Optional[str], str, Optional[str]]:
    """``(cluster_uid, cluster_name, identity source)``; no uid before the controller wrote the ledger."""
    name = _cluster_name()
    try:
        identity = await repo.get_identity(name)
    except Exception as exc:
        logger.error("Could not read the ledger cluster identity: %s", exc)
        raise HTTPException(status_code=503, detail="Savings ledger unavailable.") from exc
    return (identity[0], name, identity[1]) if identity else (None, name, None)


def _time(value: Optional[str], label: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be an ISO-8601 timestamp.") from exc


def _window(
    last: Optional[str], start: Optional[str], end: Optional[str]
) -> Tuple[Optional[datetime], Optional[datetime]]:
    if last:
        if start or end:
            raise HTTPException(status_code=400, detail="Use either last or start/end.")
        now = datetime.now(timezone.utc)
        if last.lower() == "ytd":
            return datetime(now.year, 1, 1, tzinfo=timezone.utc), now
        try:
            return now - parse_duration(last), now
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    begin, finish = _time(start, "start"), _time(end, "end")
    if (begin is None) != (finish is None):
        # One bound alone would be prorated by the ledger but ignored by the savings report.
        raise HTTPException(status_code=400, detail="start and end must be given together.")
    if begin is not None and finish is not None and finish <= begin:
        raise HTTPException(status_code=400, detail="end must be after start.")
    return begin, finish


def _choice(value: Optional[str], allowed: Tuple[str, ...], label: str) -> Optional[str]:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=422, detail=f"{label} must be one of {', '.join(allowed)}.")
    return value


class LedgerFilters:
    """Query filters shared by the listing and the exports."""

    def __init__(
        self,
        namespace: Optional[str] = Depends(validate_namespace),
        method: Optional[str] = Query(
            None, description="projected, measured_allocated, realized_billed, user_declared"
        ),
        quantity: Optional[str] = Query(None, description="cost or co2e_g"),
        entry_kind: Optional[str] = Query(None, description="accrual, reversal or restatement"),
        book: Optional[str] = Query(None, description="accrued (reconciles with v1) or restatement"),
        reason: Optional[str] = Query(None, description="accrual, drift, supersession or restatement"),
        recommendation_id: Optional[int] = Query(None, ge=1),
        change_id: Optional[str] = Query(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        measurement_id: Optional[str] = Query(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        last: Optional[str] = Query(None, description="Window ending now, e.g. 24h, 7d, 30d, ytd."),
        start: Optional[str] = Query(None, description="Window start (ISO-8601)."),
        end: Optional[str] = Query(None, description="Window end (ISO-8601)."),
    ) -> None:
        self.values: Dict[str, Any] = {
            "namespace": namespace,
            "method": _choice(method, LEDGER_METHODS, "method"),
            "quantity": _choice(quantity, QUANTITIES, "quantity"),
            "entry_kind": _choice(entry_kind, ENTRY_KINDS, "entry_kind"),
            "book": _choice(book, BOOKS, "book"),
            "reason": _choice(reason, REASONS, "reason"),
            "recommendation_id": recommendation_id,
            "change_id": change_id,
            "measurement_id": measurement_id,
        }
        self.start, self.end = _window(last, start, end)


def _cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"seq": seq}).encode()).decode().rstrip("=")


def _parse_cursor(cursor: Optional[str]) -> Optional[int]:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        seq = int(json.loads(base64.urlsafe_b64decode(padded.encode()).decode())["seq"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor.") from exc
    if seq < 1:
        raise HTTPException(status_code=400, detail="Invalid cursor.")
    return seq


def window_share(entry: LedgerEntry, start: Optional[datetime], end: Optional[datetime]) -> float:
    """Share of the entry's period inside ``[start, end)`` (1 without a window)."""
    if start is None and end is None:
        return 1.0
    begin = max(entry.period_start, start) if start is not None else entry.period_start
    finish = min(entry.period_end, end) if end is not None else entry.period_end
    length = (entry.period_end - entry.period_start).total_seconds()
    if length <= 0:
        return 1.0 if begin <= entry.period_start and entry.period_end <= finish else 0.0
    return max(0.0, (finish - begin).total_seconds()) / length


def _external_pr_url(url: Optional[str]) -> Optional[str]:
    """The pull request URL as users open it (a Gitea API origin rewritten to its web base URL)."""
    from greenkube.api.routers.automation import external_pull_request_url

    return external_pull_request_url(url)


def _entry_payload(entry: LedgerEntry) -> Dict[str, Any]:
    payload = entry.as_dict()
    payload["pr_url"] = _external_pr_url(entry.pr_url)
    return payload


@router.get("/ledger/entries")
async def list_ledger_entries(
    filters: LedgerFilters = Depends(),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(None, max_length=200, description="next_cursor of the previous page."),
    repo: LedgerRepository = Depends(get_ledger_repo),
) -> Dict[str, Any]:
    """Ledger entries, newest first (highest chain position first)."""
    before = _parse_cursor(cursor)
    cluster_uid, cluster_name, _ = await _cluster(repo)
    if cluster_uid is None:
        return {"cluster_uid": None, "cluster_name": cluster_name, "items": [], "next_cursor": None}
    try:
        rows = await repo.list_entries(
            cluster_uid,
            filters=filters.values,
            start=filters.start,
            end=filters.end,
            before_seq=before,
            limit=limit + 1,
        )
    except Exception as exc:
        logger.error("Could not read the savings ledger: %s", exc)
        raise HTTPException(status_code=503, detail="Savings ledger unavailable.") from exc
    page = rows[:limit]
    return {
        "cluster_uid": cluster_uid,
        "cluster_name": cluster_name,
        "items": [_entry_payload(entry) for entry in page],
        "next_cursor": _cursor(page[-1].seq) if len(rows) > limit and page else None,
    }


@router.get("/ledger/summary")
async def get_ledger_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Window ending now, e.g. 24h, 7d, 30d, ytd."),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    repo: LedgerRepository = Depends(get_ledger_repo),
) -> Dict[str, Any]:
    """Signed totals per method and quantity (never summed across methods) and the hash-chain status."""
    begin, finish = _window(last, start, end)
    cluster_uid, cluster_name, source = await _cluster(repo)
    base: Dict[str, Any] = {
        "cluster_uid": cluster_uid,
        "cluster_name": cluster_name,
        "cluster_uid_source": source,
        "window": {"start": format_time(begin), "end": format_time(finish)},
        "totals": [],
        "currencies": [],
        "methods": dict(METHOD_BASIS),
        "entries_by_kind": {},
        "chain": None,
    }
    if cluster_uid is None:
        return base
    try:
        rows = await repo.summary(cluster_uid, start=begin, end=finish, namespace=namespace)
        status = await repo.get_chain_status(cluster_uid)
        head_seq, head_hash = await repo.head(cluster_uid)
    except Exception as exc:
        logger.error("Could not read the savings ledger: %s", exc)
        raise HTTPException(status_code=503, detail="Savings ledger unavailable.") from exc
    # Never summed across methods, quantities or currencies: one total per (method, quantity, unit).
    totals: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    by_kind: Dict[str, int] = {}
    for row in rows:
        bucket = totals.setdefault(
            (row.method, row.quantity, row.unit),
            {
                "method": row.method,
                "quantity": row.quantity,
                "unit": row.unit,
                "currency": row.unit if row.quantity == QUANTITY_COST else None,
                "basis": METHOD_BASIS.get(row.method, "unknown"),
                "billed": row.method == "realized_billed",
                "accrued": 0.0,
                "restatement": 0.0,
            },
        )
        bucket["accrued" if row.book == BOOK_ACCRUED else "restatement"] += row.value
        by_kind[row.entry_kind] = by_kind.get(row.entry_kind, 0) + row.entries
    for bucket in totals.values():
        bucket["restated"] = bucket["accrued"] + bucket["restatement"]
    base["totals"] = sorted(totals.values(), key=lambda item: (item["method"], item["quantity"], item["unit"]))
    base["currencies"] = sorted({item["unit"] for item in totals.values() if item["quantity"] == QUANTITY_COST})
    base["entries_by_kind"] = by_kind
    base["chain"] = {
        "head_seq": head_seq or None,
        "head_hash": head_hash,
        "verified_at": status.verified_at.isoformat() if status is not None else None,
        "verified_entries": status.entries if status is not None else 0,
        "verified_head_seq": status.head_seq if status is not None else None,
        "ok": status.ok if status is not None else None,
        "failure": status.failure if status is not None else None,
    }
    return base


async def _entries(repo: LedgerRepository, cluster_uid: str, filters: LedgerFilters) -> AsyncIterator[LedgerEntry]:
    after = 0
    while True:
        page = await repo.list_entries(
            cluster_uid,
            filters=filters.values,
            start=filters.start,
            end=filters.end,
            before_seq=after,
            limit=EXPORT_PAGE,
            ascending=True,
        )
        for entry in page:
            yield entry
        if len(page) < EXPORT_PAGE:
            return
        after = page[-1].seq


#: Export columns written as numbers; every other string cell is guarded against formula injection.
NUMERIC_COLUMNS = frozenset(
    {
        "seq",
        "value",
        "window_share",
        "window_value",
        "recommendation_id",
        "confidence",
        "intensity_g_per_kwh",
        "source_id",
        "BilledCost",
        "EffectiveCost",
        "x-greenkube-saving",
        "x-greenkube-co2e-g",
        "x-greenkube-window-share",
    }
)


def _guarded(columns: Tuple[str, ...], values: Dict[str, Any]) -> List[Any]:
    """The row of ``columns``, string cells sanitized as the v1 CSV report does (``_sanitize_cell``)."""
    from greenkube.api.routers.report import _sanitize_cell

    row: List[Any] = []
    for column in columns:
        value = values.get(column)
        if isinstance(value, str) and column not in NUMERIC_COLUMNS:
            value = _sanitize_cell(value)
        row.append(value)
    return row


def _csv_line(values: List[Any]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(["" if value is None else value for value in values])
    return buffer.getvalue()


def _csv_row(entry: LedgerEntry, share: float) -> List[Any]:
    data = entry.as_dict()
    data["window_share"] = repr(share)
    data["window_value"] = repr(entry.value * share)
    data["value"] = repr(entry.value)
    data["recommendation_ids"] = json.dumps(data["recommendation_ids"], separators=(",", ":"))
    data["pr_url"] = _external_pr_url(entry.pr_url)
    return _guarded(CSV_COLUMNS, data)


def _billing_period(moment: datetime) -> Tuple[str, str]:
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return format_time(start) or "", format_time(end) or ""


def _focus_row(cost: Optional[LedgerEntry], co2e: Optional[LedgerEntry], share: float) -> List[Any]:
    entry = cost or co2e
    assert entry is not None
    saving = (cost.value if cost is not None else 0.0) * share
    billing_start, billing_end = _billing_period(entry.period_start)
    correction = entry.entry_kind != "accrual"
    description = f"GreenKube {entry.method.replace('_', ' ')} saving ({entry.entry_kind}, {entry.reason})" + (
        f" of recommendation {entry.recommendation_id}" if entry.recommendation_id is not None else ""
    )
    values: Dict[str, Any] = {
        "BillingAccountId": entry.cluster_uid,
        "BillingAccountName": entry.cluster_name,
        "SubAccountId": entry.namespace,
        "BillingPeriodStart": billing_start,
        "BillingPeriodEnd": billing_end,
        "ChargePeriodStart": format_time(entry.period_start),
        "ChargePeriodEnd": format_time(entry.period_end),
        "ChargeCategory": "Adjustment" if correction else "Credit",
        "ChargeClass": "Correction" if correction else "",
        "ChargeDescription": description,
        # Savings are avoided spend, not invoiced: only realized billed savings change the bill.
        "BilledCost": repr(-saving if entry.method == "realized_billed" else 0.0),
        "EffectiveCost": repr(-saving),
        "BillingCurrency": (cost.currency if cost is not None else None) or "",
        "ProviderName": "GreenKube",
        "PublisherName": "GreenKube",
        "ServiceName": "Kubernetes",
        "ServiceCategory": "Compute",
        "ResourceId": f"recommendation/{entry.recommendation_id}" if entry.recommendation_id is not None else "",
        "ResourceType": entry.recommendation_type,
        "Tags": json.dumps({"greenkube.io/namespace": entry.namespace}, separators=(",", ":")),
        "x-greenkube-entry-id": ";".join(item.entry_id for item in (cost, co2e) if item is not None),
        "x-greenkube-seq": ";".join(str(item.seq) for item in (cost, co2e) if item is not None),
        "x-greenkube-entry-kind": entry.entry_kind,
        "x-greenkube-book": entry.book,
        "x-greenkube-reason": entry.reason,
        "x-greenkube-method": entry.method,
        "x-greenkube-method-version": entry.method_version,
        "x-greenkube-saving": repr(saving),
        "x-greenkube-co2e-g": repr((co2e.value if co2e is not None else 0.0) * share),
        "x-greenkube-window-share": repr(share),
        "x-greenkube-measurement-id": entry.measurement_id,
        "x-greenkube-change-id": entry.change_id,
        "x-greenkube-recommendation-ids": json.dumps(entry.recommendation_ids, separators=(",", ":")),
        "x-greenkube-pr-url": _external_pr_url(entry.pr_url),
        "x-greenkube-reverses-entry-id": entry.reverses_entry_id,
        "x-greenkube-restates-entry-id": entry.restates_entry_id,
        "x-greenkube-hash": ";".join(item.hash for item in (cost, co2e) if item is not None),
    }
    return _guarded(FOCUS_COLUMNS, values)


async def _focus_lines(entries: AsyncIterator[LedgerEntry], filters: LedgerFilters) -> AsyncIterator[str]:
    """One FOCUS row per accrual group: the cost entry and the CO2e entry of the same period and method."""
    pending: Dict[Tuple[str, str, str], Dict[str, LedgerEntry]] = {}
    order: List[Tuple[str, str, str]] = []
    async for entry in entries:
        key = (entry.group_key, entry.method, entry.entry_kind)
        if key not in pending:
            pending[key] = {}
            order.append(key)
        pending[key][entry.quantity] = entry
        if QUANTITY_COST in pending[key] and QUANTITY_CO2E in pending[key]:
            group = pending.pop(key)
            order.remove(key)
            cost = group[QUANTITY_COST]
            yield _csv_line(_focus_row(cost, group[QUANTITY_CO2E], window_share(cost, filters.start, filters.end)))
        while len(order) > EXPORT_PAGE:
            stale = order.pop(0)
            group = pending.pop(stale)
            first = group.get(QUANTITY_COST) or group[QUANTITY_CO2E]
            yield _csv_line(
                _focus_row(
                    group.get(QUANTITY_COST), group.get(QUANTITY_CO2E), window_share(first, filters.start, filters.end)
                )
            )
    for key in order:
        group = pending[key]
        first = group.get(QUANTITY_COST) or group[QUANTITY_CO2E]
        yield _csv_line(
            _focus_row(
                group.get(QUANTITY_COST), group.get(QUANTITY_CO2E), window_share(first, filters.start, filters.end)
            )
        )


@router.get("/ledger/export")
async def export_ledger(
    filters: LedgerFilters = Depends(),
    format: str = Query("csv", pattern="^(csv|focus)$", description="csv (one row per entry) or focus."),
    repo: LedgerRepository = Depends(get_ledger_repo),
) -> StreamingResponse:
    """Stream the ledger as CSV, oldest entry first."""
    cluster_uid, _, _ = await _cluster(repo)
    columns = FOCUS_COLUMNS if format == "focus" else CSV_COLUMNS

    async def lines() -> AsyncIterator[str]:
        yield _csv_line(list(columns))
        if cluster_uid is None:
            return
        entries = _entries(repo, cluster_uid, filters)
        if format == "focus":
            async for line in _focus_lines(entries, filters):
                yield line
            return
        async for entry in entries:
            yield _csv_line(_csv_row(entry, window_share(entry, filters.start, filters.end)))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"greenkube-ledger-{'focus-' if format == 'focus' else ''}{stamp}.csv"
    return StreamingResponse(
        lines(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
