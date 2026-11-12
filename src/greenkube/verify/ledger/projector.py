# src/greenkube/verify/ledger/projector.py
"""Writes the savings ledger v2 from the accrual path (roadmap C6, C8, BUG-39).

The v1 ledger (``recommendation_savings_ledger``) stays the write target of
the attribution job, the drift monitor and the demo, and the source of the
legacy views. The projector turns it into chained v2 entries, in three steps
that are each idempotent (every entry has a deterministic ``entry_key``):

1. **Accruals.** Every v1 row after the projection watermark becomes one entry
   per non-zero quantity (``cost`` in the pricing currency, ``co2e_g``), with
   the v2 method (``measured`` → ``measured_allocated``) and its provenance:
   change, pull request, measurement and method version. A v1 drift reversal
   (an off-grid, negative ``measured`` row) becomes a ``reversal`` entry with
   reason ``drift`` linked to the first measured accrual it cancels. Legacy
   compressed hourly rows are projected too, so the ledger reconciles with
   ``GET /recommendations/savings``. The watermark advances in the same
   transaction as the inserts. Each pass re-reads the last
   :data:`WATERMARK_LOOKBACK` ids, so a row whose id was allocated before
   but committed after a projected one is never missed.
2. **Supersession.** A v1 row flagged ``superseded`` (rollback review,
   reverted, phantom repair) is cancelled by a ``reversal`` entry (reason
   ``supersession``), as are the restatement entries attached to it.
3. **Restatement (BUG-39).** Once a recommendation is verified, each of its
   projected accruals that ended at or before ``verified_at`` is replaced by
   the measured amount of the same period: a ``reversal`` of the projection
   plus a ``restatement`` entry (``measured_allocated``, linked to the
   projection, the measurement and the change). Both sit in the
   ``restatement`` book, so the ``accrued`` book keeps matching v1.

Values are signed and never floored. Nothing is ever updated or deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from prometheus_client import Counter

from greenkube.core.observability import REGISTRY as OBSERVABILITY_REGISTRY
from greenkube.storage.ledger_repository import V1_RAW_TABLE, V1_TABLES, LedgerRepository, Provenance
from greenkube.utils.time import ensure_utc
from greenkube.verify.ledger.identity import ClusterIdentity, ClusterIdentityResolver, ClusterIdentityUnavailable
from greenkube.verify.ledger.model import (
    BOOK_ACCRUED,
    BOOK_RESTATEMENT,
    CO2E_UNIT,
    ENTRY_ACCRUAL,
    ENTRY_RESTATEMENT,
    ENTRY_REVERSAL,
    LEDGER_WRITER_VERSION,
    METHOD_MEASURED_ALLOCATED,
    QUANTITY_CO2E,
    QUANTITY_COST,
    REASON_ACCRUAL,
    REASON_DRIFT,
    REASON_RESTATEMENT,
    REASON_SUPERSESSION,
    LedgerEntry,
    LedgerEntryDraft,
    entry_id_for,
    v2_method,
)

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 365.25 * 24 * 3600
#: v1 rows read per batch, and at most this many batches per pass (the rest catches up next pass).
BATCH_SIZE = 2000
MAX_BATCHES_PER_SYNC = 50
#: Ids re-read below the watermark on every pass (out-of-order commits of the v1 sequence).
WATERMARK_LOOKBACK = 1000
#: Statuses whose projected accruals are restated by their measurement.
RESTATED_STATUSES = ("verified", "regressed")

LEDGER_WRITE_ERRORS = Counter(
    "greenkube_ledger_write_errors_total",
    "Savings ledger v2 passes that could not write, by reason (the v1 ledger is not affected).",
    ["reason"],
    registry=OBSERVABILITY_REGISTRY,
)

RecordsProvider = Callable[[], Awaitable[Sequence[Any]]]


@dataclass
class SyncResult:
    """What one projector pass wrote."""

    cluster_uid: Optional[str] = None
    projected: int = 0
    superseded: int = 0
    restated: int = 0
    skipped: Optional[str] = None
    watermarks: Dict[str, int] = field(default_factory=dict)

    @property
    def written(self) -> int:
        return self.projected + self.superseded + self.restated


def _value(raw: Any) -> str:
    return str(getattr(raw, "value", raw) or "")


def _is_drift_reversal(table: str, row: dict, method: str) -> bool:
    """A v1 reversal written by the drift monitor: off the accrual grid, measured, negative."""
    if table != V1_RAW_TABLE or row.get("slot_start") is not None or method != METHOD_MEASURED_ALLOCATED:
        return False
    co2e = float(row.get("co2e_saved_grams") or 0.0)
    cost = float(row.get("cost_saved_dollars") or 0.0)
    return co2e <= 0 and cost <= 0 and (co2e < 0 or cost < 0)


class LedgerProjector:
    """Projects the v1 ledger into the chained v2 ledger of one cluster."""

    def __init__(
        self,
        repo: LedgerRepository,
        resolver: ClusterIdentityResolver,
        *,
        records: Optional[RecordsProvider] = None,
        currency: str = "USD",
        batch_size: int = BATCH_SIZE,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.repo = repo
        self.resolver = resolver
        self._records = records
        self.currency = currency
        self.batch_size = max(1, int(batch_size))
        self._clock = clock

    def _now(self) -> datetime:
        return ensure_utc(self._clock()) if self._clock is not None else datetime.now(timezone.utc)

    async def sync(self) -> SyncResult:
        """One pass: accruals, then supersessions, then restatements."""
        try:
            identity = await self.resolver.resolve()
        except ClusterIdentityUnavailable as exc:
            logger.info("Savings ledger v2 pass skipped: %s", exc)
            return SyncResult(skipped="cluster_identity_unavailable")
        result = SyncResult(cluster_uid=identity.cluster_uid)
        for step, run in (
            ("projection", self._project),
            ("supersession", self._supersede),
            ("restatement", self._restate),
        ):
            try:
                await run(identity, result)
            except Exception as exc:
                LEDGER_WRITE_ERRORS.labels(reason=f"{step}_failed").inc()
                logger.error("Savings ledger v2 %s failed: %s", step, exc)
                result.skipped = f"{step}_failed"
                break
        return result

    # ------------------------------------------------------------------
    # 1. Accruals from the v1 ledger
    # ------------------------------------------------------------------

    async def _project(self, identity: ClusterIdentity, result: SyncResult) -> None:
        watermarks = await self.repo.get_watermarks(identity.cluster_uid)
        for table in V1_TABLES:
            position = watermarks.get(table, 0)
            after = max(0, position - WATERMARK_LOOKBACK)
            for _ in range(MAX_BATCHES_PER_SYNC):
                rows = await self.repo.read_v1_rows(table, identity.cluster_name, after, self.batch_size)
                if not rows:
                    break
                drafts = await self._accrual_drafts(identity, table, rows)
                last = max(int(row["id"]) for row in rows)
                inserted = await self.repo.append(
                    identity.cluster_uid,
                    identity.cluster_name,
                    drafts,
                    watermarks={table: max(last, position)},
                    now=self._now(),
                )
                result.projected += len(inserted)
                result.watermarks[table] = max(last, position)
                after = last
                if len(rows) < self.batch_size:
                    break

    async def _accrual_drafts(
        self, identity: ClusterIdentity, table: str, rows: Sequence[dict]
    ) -> List[LedgerEntryDraft]:
        provenance = await self.repo.provenance(int(row["recommendation_id"]) for row in rows)
        drafts: List[LedgerEntryDraft] = []
        for row in rows:
            try:
                drafts.extend(await self._row_drafts(identity, table, row, provenance, drafts))
            except (TypeError, ValueError) as exc:
                LEDGER_WRITE_ERRORS.labels(reason="invalid_v1_row").inc()
                logger.warning("Savings ledger v2 skipped %s row %s: %s", table, row.get("id"), exc)
        return drafts

    async def _row_drafts(
        self,
        identity: ClusterIdentity,
        table: str,
        row: dict,
        provenance: Dict[int, Provenance],
        pending: Sequence[LedgerEntryDraft],
    ) -> List[LedgerEntryDraft]:
        drafts: List[LedgerEntryDraft] = []
        rec_id = int(row["recommendation_id"])
        method = v2_method(row.get("measurement_method"))
        source = provenance.get(rec_id, Provenance())
        drift = _is_drift_reversal(table, row, method)
        reverses: Optional[str] = None
        if drift:
            reverses = await self._first_reversed(identity, rec_id, row, pending)
        measured = method == METHOD_MEASURED_ALLOCATED
        for quantity, amount, unit, currency in (
            (QUANTITY_COST, row.get("cost_saved_dollars"), self.currency, self.currency),
            (QUANTITY_CO2E, row.get("co2e_saved_grams"), CO2E_UNIT, None),
        ):
            value = float(amount or 0.0)
            if value == 0.0:
                continue
            drafts.append(
                LedgerEntryDraft(
                    entry_key=f"v1:{table}:{row['id']}:{quantity}",
                    group_key=f"v1:{table}:{row['id']}",
                    entry_kind=ENTRY_REVERSAL if drift else ENTRY_ACCRUAL,
                    book=BOOK_ACCRUED,
                    reason=REASON_DRIFT if drift else REASON_ACCRUAL,
                    method=method,
                    quantity=quantity,
                    value=value,
                    unit=unit,
                    currency=currency,
                    method_version=(source.method_version if measured and source.method_version else None)
                    or f"{LEDGER_WRITER_VERSION}:{row.get('measurement_method') or 'projected'}",
                    period_start=row["period_start"],
                    period_end=row["period_end"],
                    slot_start=row.get("slot_start"),
                    created_by="drift-monitor" if drift else "savings-attributor",
                    recommendation_id=rec_id,
                    recommendation_ids=list(source.recommendation_ids or (rec_id,)),
                    namespace=row.get("namespace") or "",
                    recommendation_type=row.get("recommendation_type") or "",
                    change_id=source.change_id,
                    measurement_id=source.measurement_id if measured else None,
                    pr_url=source.pr_url,
                    confidence=float(row["confidence"]) if row.get("confidence") is not None else None,
                    reverses_entry_id=reverses,
                    source_table=table,
                    source_id=int(row["id"]),
                )
            )
        return drafts

    async def _first_reversed(
        self, identity: ClusterIdentity, rec_id: int, row: dict, pending: Sequence[LedgerEntryDraft]
    ) -> Optional[str]:
        """The first measured accrual a drift reversal cancels (stored, or in the same batch)."""
        start, end = row["period_start"], row["period_end"]
        stored = await self.repo.measured_accruals(identity.cluster_uid, rec_id, start, end)
        if stored:
            return stored[0].entry_id
        for draft in pending:
            if (
                draft.recommendation_id == rec_id
                and draft.method == METHOD_MEASURED_ALLOCATED
                and draft.entry_kind == ENTRY_ACCRUAL
                and draft.period_end > start
                and draft.period_start < end
            ):
                return entry_id_for(identity.cluster_uid, draft.entry_key)
        return None

    # ------------------------------------------------------------------
    # 2. Supersession
    # ------------------------------------------------------------------

    async def _supersede(self, identity: ClusterIdentity, result: SyncResult) -> None:
        targets = await self.repo.superseded_unreversed(identity.cluster_uid)
        if not targets:
            return
        drafts = [reversal_of(entry, REASON_SUPERSESSION, created_by="ledger-projector") for entry in targets]
        inserted = await self.repo.append(identity.cluster_uid, identity.cluster_name, drafts, now=self._now())
        result.superseded += len(inserted)

    # ------------------------------------------------------------------
    # 3. Restatement of projections by measurements (BUG-39)
    # ------------------------------------------------------------------

    async def _restate(self, identity: ClusterIdentity, result: SyncResult) -> None:
        if self._records is None:
            return
        records = {
            int(record.id): record
            for record in await self._records()
            if record.id is not None
            and _value(record.status) in RESTATED_STATUSES
            and record.verified_at is not None
            and (record.measured_cost_saved is not None or record.measured_co2e_saved_grams is not None)
        }
        if not records:
            return
        projections = await self.repo.unrestated_projections(identity.cluster_uid, records)
        if not projections:
            return
        superseded = await self.repo.superseded_sources(projections)
        provenance = await self.repo.provenance(records)
        drafts: List[LedgerEntryDraft] = []
        for projection in projections:
            if projection.entry_id in superseded or projection.recommendation_id is None:
                continue
            record = records.get(projection.recommendation_id)
            if record is None or projection.period_end > ensure_utc(record.verified_at):
                continue
            annual = (
                record.measured_cost_saved if projection.quantity == QUANTITY_COST else record.measured_co2e_saved_grams
            )
            if annual is None:
                continue
            drafts.extend(
                restatement_of(
                    projection,
                    annual_rate=float(annual),
                    provenance=provenance.get(projection.recommendation_id, Provenance()),
                )
            )
        if drafts:
            inserted = await self.repo.append(identity.cluster_uid, identity.cluster_name, drafts, now=self._now())
            result.restated += len(inserted)


def reversal_of(entry: LedgerEntry, reason: str, *, created_by: str) -> LedgerEntryDraft:
    """The entry cancelling ``entry`` (same book, period and method, opposite value)."""
    return LedgerEntryDraft(
        entry_key=f"reversal:{reason}:{entry.entry_id}",
        group_key=f"reversal:{reason}:{entry.group_key}",
        entry_kind=ENTRY_REVERSAL,
        book=entry.book,
        reason=reason,
        method=entry.method,
        quantity=entry.quantity,
        value=-entry.value,
        unit=entry.unit,
        currency=entry.currency,
        method_version=LEDGER_WRITER_VERSION,
        period_start=entry.period_start,
        period_end=entry.period_end,
        slot_start=entry.slot_start,
        created_by=created_by,
        recommendation_id=entry.recommendation_id,
        recommendation_ids=list(entry.recommendation_ids),
        namespace=entry.namespace,
        recommendation_type=entry.recommendation_type,
        change_id=entry.change_id,
        measurement_id=entry.measurement_id,
        pr_url=entry.pr_url,
        reverses_entry_id=entry.entry_id,
    )


def restatement_of(projection: LedgerEntry, *, annual_rate: float, provenance: Provenance) -> List[LedgerEntryDraft]:
    """The reversal of a projected accrual and the measured amount of its period, linked to it."""
    seconds = (ensure_utc(projection.period_end) - ensure_utc(projection.period_start)).total_seconds()
    reversal = reversal_of(projection, REASON_RESTATEMENT, created_by="ledger-restatement")
    reversal = LedgerEntryDraft(
        **{
            **{name: getattr(reversal, name) for name in LedgerEntryDraft.__dataclass_fields__},
            "book": BOOK_RESTATEMENT,
            "change_id": provenance.change_id or projection.change_id,
            "measurement_id": provenance.measurement_id,
        }
    )
    measured = LedgerEntryDraft(
        entry_key=f"restatement:{projection.entry_id}",
        group_key=f"restatement:{projection.group_key}",
        entry_kind=ENTRY_RESTATEMENT,
        book=BOOK_RESTATEMENT,
        reason=REASON_RESTATEMENT,
        method=METHOD_MEASURED_ALLOCATED,
        quantity=projection.quantity,
        value=annual_rate * seconds / SECONDS_PER_YEAR,
        unit=projection.unit,
        currency=projection.currency,
        method_version=provenance.method_version or LEDGER_WRITER_VERSION,
        period_start=projection.period_start,
        period_end=projection.period_end,
        slot_start=projection.slot_start,
        created_by="ledger-restatement",
        recommendation_id=projection.recommendation_id,
        recommendation_ids=list(provenance.recommendation_ids or projection.recommendation_ids),
        namespace=projection.namespace,
        recommendation_type=projection.recommendation_type,
        change_id=provenance.change_id or projection.change_id,
        measurement_id=provenance.measurement_id,
        pr_url=provenance.pr_url or projection.pr_url,
        restates_entry_id=projection.entry_id,
    )
    return [reversal, measured]


def build_projector(
    *,
    db_manager: Any = None,
    cluster_name: Optional[str] = None,
    records: Optional[RecordsProvider] = None,
    reader: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
) -> LedgerProjector:
    """The projector of this process's cluster (the shared database by default)."""
    from greenkube.core.config import get_config
    from greenkube.core.savings_attributor import savings_cluster_name
    from greenkube.storage.ledger_repository import get_ledger_repository

    repo = get_ledger_repository(db_manager)
    if records is None:

        async def records() -> Sequence[Any]:
            from greenkube.core.factory import get_recommendation_repository

            return await get_recommendation_repository().get_recommendations_by_statuses(list(RESTATED_STATUSES))

    resolver = ClusterIdentityResolver(repo, cluster_name or savings_cluster_name(), reader=reader)
    currency = str(getattr(get_config(), "IMPACT_PRICING_CURRENCY", "USD") or "USD")
    return LedgerProjector(repo, resolver, records=records, currency=currency)


__all__ = [
    "LEDGER_WRITE_ERRORS",
    "LedgerProjector",
    "SyncResult",
    "build_projector",
    "restatement_of",
    "reversal_of",
]
