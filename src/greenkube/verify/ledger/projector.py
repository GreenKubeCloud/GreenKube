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
   transaction as the inserts. Rows are found with an anti-join (v1 rows
   that have no entry yet), never by id alone: a v1 id can be allocated by a
   long transaction and committed after higher ids were projected (the
   attribution job and the drift monitor write in separate transactions).
   An incremental pass starts :data:`WATERMARK_LOOKBACK` ids below the
   watermark; a full sweep (every :data:`FULL_SWEEP_INTERVAL` per projector,
   and on every ``ledger-chain`` tick) starts at the first id, so no row is
   ever skipped, and ``greenkube_ledger_unprojected_rows`` reports what is
   still missing after it.
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
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from prometheus_client import Counter, Gauge

from greenkube.core.observability import REGISTRY as OBSERVABILITY_REGISTRY
from greenkube.core.savings_attributor import is_non_representative
from greenkube.models.savings import DRIFT_REVERSAL_ROW_KIND
from greenkube.storage.ledger_repository import V1_RAW_TABLE, V1_TABLES, LedgerRepository, Provenance
from greenkube.utils.time import ensure_utc
from greenkube.verify.ledger.identity import ClusterIdentity, ClusterIdentityResolver, ClusterIdentityUnavailable
from greenkube.verify.ledger.model import (
    BASIS_ALLOCATED,
    BOOK_ACCRUED,
    BOOK_RESTATEMENT,
    CAPACITY_UNITS,
    CO2E_UNIT,
    ENTRY_ACCRUAL,
    ENTRY_RESTATEMENT,
    ENTRY_REVERSAL,
    LEDGER_WRITER_VERSION,
    METHOD_MEASURED_ALLOCATED,
    QUANTITY_CO2E,
    QUANTITY_COST,
    QUANTITY_CPU_CORE_HOURS,
    QUANTITY_MEM_GIB_HOURS,
    REASON_ACCRUAL,
    REASON_COST_OF_FIX,
    REASON_DRIFT,
    REASON_NOT_REALIZED,
    REASON_REINSTATEMENT,
    REASON_RELEASED_CAPACITY,
    REASON_RESTATEMENT,
    REASON_SUPERSESSION,
    LedgerEntry,
    LedgerEntryDraft,
    basis_of,
    entry_id_for,
    format_time,
    v2_method,
)

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 365.25 * 24 * 3600
#: The accrual grid (one-hour slots, as the attribution job).
SLOT_SECONDS = 3600
#: Released-capacity and cost-of-fix slots written per recommendation and quantity by one pass.
MAX_MEASURED_SLOTS_PER_PASS = 24 * 31
#: v1 rows read per batch, and at most this many batches per pass (the rest catches up next pass).
BATCH_SIZE = 2000
MAX_BATCHES_PER_SYNC = 50
#: Ids re-read below the watermark by an incremental pass (out-of-order commits of the v1 sequence).
WATERMARK_LOOKBACK = 1000
#: A projector sweeps the whole v1 ledger (anti-join from the first id) at least this often.
FULL_SWEEP_INTERVAL = timedelta(hours=1)
#: Statuses whose projected accruals are restated by their measurement.
RESTATED_STATUSES = ("verified", "regressed")
#: Statuses whose accruals are all superseded on every pass (LED-08): review, revert, failure, phantom repair.
SUPERSEDED_STATUSES = ("rollback_review", "reverted", "failed", "stale")
#: A dismissed rollback review returns the recommendation to applied (pending verification): a supersession
#: of a recommendation in these statuses was a dismissed review and is reinstated. A verified recommendation
#: is never reinstated (it never leaves review that way).
REINSTATED_STATUSES = ("applied", "verifying")
ACCRUING_STATUSES = ("applied", "verifying")
#: Verification statuses of a not-realized verdict (the record stays applied, LED-03).
REFUTED_VERIFICATIONS = ("failed",)
#: Every status the projector reads.
PROJECTOR_STATUSES = tuple(
    dict.fromkeys((*RESTATED_STATUSES, *SUPERSEDED_STATUSES, *REINSTATED_STATUSES, *ACCRUING_STATUSES))
)

LEDGER_WRITE_ERRORS = Counter(
    "greenkube_ledger_write_errors_total",
    "Savings ledger v2 passes that could not write, by reason (the v1 ledger is not affected).",
    ["reason"],
    registry=OBSERVABILITY_REGISTRY,
)

LEDGER_LATE_ROWS = Counter(
    "greenkube_ledger_late_rows_projected_total",
    "v1 ledger rows found by a full sweep below the incremental window (committed out of order), by table.",
    ["source_table"],
    registry=OBSERVABILITY_REGISTRY,
)
LEDGER_UNRECONCILED_RECOMMENDATIONS = Gauge(
    "greenkube_ledger_unreconciled_recommendations",
    "Recommendations under review, reverted, failed or stale whose accruals were still live at the last ledger "
    "pass (the pass supersedes them: a non-zero value means the one-shot supersession failed or raced).",
    registry=OBSERVABILITY_REGISTRY,
)
LEDGER_UNPROJECTED_ROWS = Gauge(
    "greenkube_ledger_unprojected_rows",
    "v1 ledger rows with a non-zero saving that have no savings ledger v2 entry after the last full sweep.",
    ["source_table"],
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


def _uncovered(
    begin: datetime, finish: datetime, periods: Sequence[tuple[datetime, datetime]]
) -> Optional[tuple[datetime, datetime, float]]:
    """The hull and length (seconds) of ``[begin, finish)`` minus ``periods``; None when fully covered."""
    pieces = [(begin, finish)]
    for start, end in periods:
        start, end = ensure_utc(start), ensure_utc(end)
        next_pieces = []
        for low, high in pieces:
            if end <= low or start >= high:
                next_pieces.append((low, high))
                continue
            if low < start:
                next_pieces.append((low, start))
            if end < high:
                next_pieces.append((end, high))
        pieces = next_pieces
    seconds = sum((high - low).total_seconds() for low, high in pieces)
    if seconds <= 0:
        return None
    return pieces[0][0], pieces[-1][1], seconds


def _floor_slot(moment: datetime) -> datetime:
    moment = ensure_utc(moment)
    return moment - timedelta(seconds=moment.timestamp() % SLOT_SECONDS)


def _is_drift_reversal(table: str, row: dict, method: str) -> bool:
    """A v1 reversal written by the drift monitor.

    Rows written since migration 0291 say so (``row_kind``), whatever the
    signs of their quantities (LED-12). Older rows are recognised as before:
    off the accrual grid, measured, negative.
    """
    if table != V1_RAW_TABLE or method != METHOD_MEASURED_ALLOCATED:
        return False
    kind = row.get("row_kind")
    if kind is not None:
        return kind == DRIFT_REVERSAL_ROW_KIND
    if row.get("slot_start") is not None:
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
        self._last_full_sweep: Optional[datetime] = None
        self._pass_records: Optional[List[Any]] = None

    async def _records_of_pass(self) -> List[Any]:
        """The recommendations of this pass (read once per pass)."""
        if self._records is None:
            return []
        if self._pass_records is None:
            self._pass_records = [record for record in await self._records() if record.id is not None]
        return self._pass_records

    def _now(self) -> datetime:
        return ensure_utc(self._clock()) if self._clock is not None else datetime.now(timezone.utc)

    async def sync(self, *, full: Optional[bool] = None) -> SyncResult:
        """One pass: accruals, supersessions, restatements, refutations and reinstatements.

        Every correction is derived from the recommendation state on each pass
        (LED-03, LED-08), so a failed or interrupted pass is caught up by the
        next one and running a pass twice writes nothing new.

        ``full`` forces (True) or skips (False) the full sweep of the v1
        ledger; by default it runs when the last one is older than
        :data:`FULL_SWEEP_INTERVAL`.
        """
        now = self._now()
        if full is None:
            full = self._last_full_sweep is None or now - self._last_full_sweep >= FULL_SWEEP_INTERVAL
        try:
            identity = await self.resolver.resolve()
        except ClusterIdentityUnavailable as exc:
            logger.info("Savings ledger v2 pass skipped: %s", exc)
            return SyncResult(skipped="cluster_identity_unavailable")
        result = SyncResult(cluster_uid=identity.cluster_uid)

        async def project(identity: ClusterIdentity, result: SyncResult) -> None:
            await self._project(identity, result, full=bool(full))
            if full:
                self._last_full_sweep = now

        self._pass_records = None
        for step, run in (
            ("projection", project),
            ("state_supersession", self._supersede_by_state),
            ("supersession", self._supersede),
            ("restatement", self._restate),
            ("refutation", self._refute),
            ("reinstatement", self._reinstate),
            ("measured_quantities", self._measured_quantities),
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

    async def _project(self, identity: ClusterIdentity, result: SyncResult, *, full: bool = False) -> None:
        watermarks = await self.repo.get_watermarks(identity.cluster_uid)
        for table in V1_TABLES:
            position = watermarks.get(table, 0)
            window_floor = max(0, position - WATERMARK_LOOKBACK)
            after = 0 if full else window_floor
            for _ in range(MAX_BATCHES_PER_SYNC):
                rows = await self.repo.read_v1_rows(
                    table, identity.cluster_name, after, self.batch_size, unprojected_for=identity.cluster_uid
                )
                if not rows:
                    break
                late = sum(1 for row in rows if int(row["id"]) <= window_floor)
                if late:
                    LEDGER_LATE_ROWS.labels(source_table=table).inc(late)
                    logger.warning(
                        "Savings ledger v2 projected %d %s row(s) committed after higher ids (below id %d).",
                        late,
                        table,
                        window_floor,
                    )
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
                position = max(last, position)
                result.watermarks[table] = position
                after = last
                if len(rows) < self.batch_size:
                    break
            if full:
                # Rows skipped as invalid, or left for the next pass by the batch cap, stay unprojected.
                backlog = await self.repo.count_unprojected_v1_rows(table, identity.cluster_name, identity.cluster_uid)
                LEDGER_UNPROJECTED_ROWS.labels(source_table=table).set(backlog)

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
        # The currency the recommendation was priced in, not today's configuration (a later currency
        # change must not relabel past accruals); the configured currency only when it is unknown.
        # Rows written since ledger/4 carry the currency and measurement read at accrual time (LED-11).
        currency_of_row = row.get("currency") or source.currency or self.currency
        measurement_of_row = row.get("measurement_id") or source.measurement_id
        for quantity, amount, unit, currency, rate in (
            (
                QUANTITY_COST,
                row.get("cost_saved_dollars"),
                currency_of_row,
                currency_of_row,
                row.get("annual_cost_rate"),
            ),
            (QUANTITY_CO2E, row.get("co2e_saved_grams"), CO2E_UNIT, None, row.get("annual_co2e_rate")),
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
                    measurement_id=measurement_of_row if measured else None,
                    pr_url=source.pr_url,
                    confidence=float(row["confidence"]) if row.get("confidence") is not None else None,
                    annual_rate=float(rate) if rate is not None and not drift else None,
                    reverses_entry_id=reverses,
                    source_table=table,
                    source_id=int(row["id"]),
                    basis=basis_of(method, row.get("basis")),
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
        records = {
            int(record.id): record
            for record in await self._records_of_pass()
            if _value(record.status) in RESTATED_STATUSES
            and record.verified_at is not None
            and (record.measured_cost_saved is not None or record.measured_co2e_saved_grams is not None)
            # A demo verdict (accelerated windows, MEA-10) restates nothing.
            and not is_non_representative(record)
        }
        if not records:
            return
        projections = await self.repo.unrestated_projections(identity.cluster_uid, records)
        superseded = await self.repo.superseded_sources(projections) if projections else set()
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
        drafts.extend(await self._slot_restatements(identity, records, provenance))
        drafts.extend(await self._remeasured(identity, records, provenance))
        if drafts:
            inserted = await self.repo.append(identity.cluster_uid, identity.cluster_name, drafts, now=self._now())
            result.restated += len(inserted)

    async def _remeasured(
        self, identity: ClusterIdentity, records: Dict[int, Any], provenance: Dict[int, Provenance]
    ) -> List[LedgerEntryDraft]:
        """Restate again from the latest measurement (LED-03).

        A restatement written from an earlier measurement (another
        ``measurement_id``) is reversed and replaced at the current measured
        rate (key ``...:{measurement_id}``). A projection refuted by an earlier
        ``not_realized`` verdict and now verified by a re-measurement gets its
        measured amount (its projection stays reversed).
        """
        drafts: List[LedgerEntryDraft] = []
        for entry in await self.repo.active_measurement_corrections(identity.cluster_uid, records):
            rec_id = entry.recommendation_id
            record = records.get(rec_id) if rec_id is not None else None
            if record is None or rec_id is None:
                continue
            source = provenance.get(rec_id, Provenance())
            current = source.measurement_id
            if entry.reason == REASON_NOT_REALIZED:
                if entry.period_end > ensure_utc(record.verified_at):
                    continue
                target_key = f"restatement:{entry.reverses_entry_id}:{current or 'latest'}"
            elif current is None or entry.measurement_id == current:
                continue
            elif entry.restates_entry_id:
                target_key = f"restatement:{entry.restates_entry_id}:{current}"
            else:
                slot = format_time(entry.slot_start or entry.period_start)
                target_key = f"restatement:slot:{rec_id}:{slot}:{entry.quantity}:{current}"
            if target_key == entry.entry_key:
                continue
            annual = record.measured_cost_saved if entry.quantity == QUANTITY_COST else record.measured_co2e_saved_grams
            if annual is None:
                continue
            if entry.reason != REASON_NOT_REALIZED:
                drafts.append(
                    replace(
                        reversal_of(entry, REASON_RESTATEMENT, created_by="ledger-restatement"),
                        book=BOOK_RESTATEMENT,
                        measurement_id=current,
                    )
                )
            seconds = (ensure_utc(entry.period_end) - ensure_utc(entry.period_start)).total_seconds()
            drafts.append(
                LedgerEntryDraft(
                    entry_key=target_key,
                    group_key=f"{target_key}:group",
                    entry_kind=ENTRY_RESTATEMENT,
                    book=BOOK_RESTATEMENT,
                    reason=REASON_RESTATEMENT,
                    method=METHOD_MEASURED_ALLOCATED,
                    quantity=entry.quantity,
                    value=float(annual) * seconds / SECONDS_PER_YEAR,
                    unit=entry.unit,
                    currency=entry.currency,
                    method_version=source.method_version or LEDGER_WRITER_VERSION,
                    period_start=entry.period_start,
                    period_end=entry.period_end,
                    slot_start=entry.slot_start,
                    created_by="ledger-restatement",
                    recommendation_id=rec_id,
                    recommendation_ids=list(source.recommendation_ids or entry.recommendation_ids),
                    namespace=entry.namespace,
                    recommendation_type=entry.recommendation_type,
                    change_id=source.change_id or entry.change_id,
                    measurement_id=current,
                    pr_url=source.pr_url or entry.pr_url,
                    restates_entry_id=entry.restates_entry_id or entry.reverses_entry_id,
                    basis=BASIS_ALLOCATED,
                    annual_rate=float(annual),
                )
            )
        return drafts

    async def _slot_restatements(
        self,
        identity: ClusterIdentity,
        records: Dict[int, Any],
        provenance: Dict[int, Provenance],
        *,
        ends: Optional[Dict[int, datetime]] = None,
        losses_only: bool = False,
    ) -> List[LedgerEntryDraft]:
        """Measured amounts for the slots of ``[applied_at, verified_at)`` that no entry covers (LED-01 c).

        A projection of exactly 0 (or one never accrued: catch-up limits,
        downtime) writes no projected entry, so there is nothing to restate,
        yet the measurement proved the saving of that period. Each such slot
        gets a ``restatement`` entry at the measured rate (key
        ``restatement:slot:{rec}:{slot}:{quantity}``). A slot is covered by a
        projected or measured accrual of the same quantity, or by an earlier
        slot restatement.
        """
        covered = await self.repo.covered_slots(identity.cluster_uid, records)
        drafts: List[LedgerEntryDraft] = []
        step = timedelta(seconds=SLOT_SECONDS)
        for rec_id, record in records.items():
            if record.applied_at is None:
                continue
            start = ensure_utc(record.applied_at)
            stop = ends.get(rec_id) if ends is not None else record.verified_at
            if stop is None:
                continue
            end = ensure_utc(stop)
            if end <= start:
                continue
            source = provenance.get(rec_id, Provenance())
            currency = source.currency or self.currency
            cursor = _floor_slot(start)
            while cursor < end:
                begin, finish = max(cursor, start), min(cursor + step, end)
                for quantity, annual, unit, unit_currency in (
                    (QUANTITY_COST, record.measured_cost_saved, currency, currency),
                    (QUANTITY_CO2E, record.measured_co2e_saved_grams, CO2E_UNIT, None),
                ):
                    if annual is None or float(annual) == 0.0 or (losses_only and float(annual) > 0.0):
                        continue
                    gap = _uncovered(begin, finish, covered.get((rec_id, quantity, cursor), ()))
                    if gap is None:
                        continue
                    gap_start, gap_end, seconds = gap
                    key = f"restatement:slot:{rec_id}:{format_time(cursor)}:{quantity}"
                    drafts.append(
                        LedgerEntryDraft(
                            entry_key=key,
                            group_key=f"restatement:slot:{rec_id}:{format_time(cursor)}",
                            entry_kind=ENTRY_RESTATEMENT,
                            book=BOOK_RESTATEMENT,
                            reason=REASON_RESTATEMENT,
                            method=METHOD_MEASURED_ALLOCATED,
                            quantity=quantity,
                            value=float(annual) * seconds / SECONDS_PER_YEAR,
                            unit=unit,
                            currency=unit_currency,
                            method_version=source.method_version or LEDGER_WRITER_VERSION,
                            period_start=gap_start,
                            period_end=gap_end,
                            slot_start=cursor,
                            created_by="ledger-restatement",
                            recommendation_id=rec_id,
                            recommendation_ids=list(source.recommendation_ids or (rec_id,)),
                            namespace=getattr(record, "namespace", None) or "",
                            recommendation_type=_value(getattr(record, "type", "")),
                            change_id=source.change_id,
                            measurement_id=source.measurement_id,
                            pr_url=source.pr_url,
                            basis=BASIS_ALLOCATED,
                        )
                    )
                cursor += step
        return drafts

    # ------------------------------------------------------------------
    # 4. Supersession derived from the recommendation state (LED-08)
    # ------------------------------------------------------------------

    async def _supersede_by_state(self, identity: ClusterIdentity, result: SyncResult) -> None:
        """Supersede every accrual of recommendations in a superseding state, on every pass.

        The one-shot call of the lifecycle loop can fail or race with an
        accrual written after it; this step flags the v1 rows that are still
        live (the next supersession step reverses their entries) and reverses
        the corrections that have no v1 row (slot restatements, reinstatements).
        """
        ids = [
            int(record.id) for record in await self._records_of_pass() if _value(record.status) in SUPERSEDED_STATUSES
        ]
        if not ids:
            LEDGER_UNRECONCILED_RECOMMENDATIONS.set(0)
            return
        live = await self.repo.live_v1_recommendations(ids)
        # The one-shot supersession of the lifecycle loop failed, or an accrual landed after it (LED-08).
        LEDGER_UNRECONCILED_RECOMMENDATIONS.set(len(live))
        flagged = await self.repo.supersede_v1(live) if live else 0
        if flagged:
            logger.info("Savings ledger v2: superseded %d v1 row(s) of recommendations under review or ended.", flagged)
        corrections = await self.repo.unreversed_corrections(identity.cluster_uid, ids)
        if corrections:
            drafts = [reversal_of(entry, REASON_SUPERSESSION, created_by="ledger-projector") for entry in corrections]
            inserted = await self.repo.append(identity.cluster_uid, identity.cluster_name, drafts, now=self._now())
            result.superseded += len(inserted)

    # ------------------------------------------------------------------
    # 5. Refuted projections and measured losses (LED-03)
    # ------------------------------------------------------------------

    async def _refute(self, identity: ClusterIdentity, result: SyncResult) -> None:
        """Reverse the projections a measurement refuted (``not_realized``) and book a measured loss.

        Each unreversed projected accrual of a not-realized recommendation is
        reversed (reason ``not_realized``, ``restatement`` book). When the
        measurement found a signed loss (negative annual rate) the projection
        is restated at that rate instead (``measured_allocated``, negative),
        and the uncovered slots up to the end of the measurement window are
        booked at it too. A positive rate that the measurement could not
        claim is never booked. Inconclusive verdicts keep their projection
        (reported apart as unverified).
        """
        records = {
            int(record.id): record
            for record in await self._records_of_pass()
            if _value(record.status) in ACCRUING_STATUSES
            and _value(record.verification_status) in REFUTED_VERIFICATIONS
        }
        if not records:
            return
        projections = await self.repo.unrestated_projections(identity.cluster_uid, records)
        superseded = await self.repo.superseded_sources(projections) if projections else set()
        provenance = await self.repo.provenance(records)
        drafts: List[LedgerEntryDraft] = []
        for projection in projections:
            if projection.entry_id in superseded or projection.recommendation_id is None:
                continue
            record = records.get(projection.recommendation_id)
            if record is None:
                continue
            annual = (
                record.measured_cost_saved if projection.quantity == QUANTITY_COST else record.measured_co2e_saved_grams
            )
            source = provenance.get(projection.recommendation_id, Provenance())
            if annual is not None and float(annual) < 0.0 and not is_non_representative(record):
                drafts.extend(restatement_of(projection, annual_rate=float(annual), provenance=source))
                continue
            reversal = reversal_of(projection, REASON_NOT_REALIZED, created_by="ledger-refutation")
            drafts.append(
                replace(
                    reversal,
                    book=BOOK_RESTATEMENT,
                    change_id=source.change_id or projection.change_id,
                    measurement_id=source.measurement_id,
                )
            )
        losses = {
            rec_id: record
            for rec_id, record in records.items()
            if not is_non_representative(record)
            and any(
                value is not None and float(value) < 0.0
                for value in (record.measured_cost_saved, record.measured_co2e_saved_grams)
            )
        }
        ends = {
            rec_id: record.verification_window_end
            for rec_id, record in losses.items()
            if getattr(record, "verification_window_end", None) is not None
        }
        if ends:
            drafts.extend(await self._slot_restatements(identity, losses, provenance, ends=ends, losses_only=True))
        if drafts:
            inserted = await self.repo.append(identity.cluster_uid, identity.cluster_name, drafts, now=self._now())
            result.restated += len(inserted)

    # ------------------------------------------------------------------
    # 6. Reinstatement after a dismissed rollback review (LED-08)
    # ------------------------------------------------------------------

    async def _reinstate(self, identity: ClusterIdentity, result: SyncResult) -> None:
        """Cancel the supersession reversals of recommendations that accrue again (a dismissed review).

        The v1 ``superseded`` flag cannot be unset (append-only), so the
        reinstatement lives in the ``restatement`` book: the ``accrued`` book
        keeps mirroring v1 and the restated total re-credits the accruals
        written before the review.
        """
        ids = [
            int(record.id) for record in await self._records_of_pass() if _value(record.status) in REINSTATED_STATUSES
        ]
        if not ids:
            return
        reversals = await self.repo.unreinstated_supersessions(identity.cluster_uid, ids)
        drafts = [
            replace(reversal_of(entry, REASON_REINSTATEMENT, created_by="ledger-projector"), book=BOOK_RESTATEMENT)
            for entry in reversals
        ]
        if drafts:
            inserted = await self.repo.append(identity.cluster_uid, identity.cluster_name, drafts, now=self._now())
            result.superseded += len(inserted)

    # ------------------------------------------------------------------
    # 7. Released capacity and the cost of a reliability fix (WP-08.1, MEA-14)
    # ------------------------------------------------------------------

    async def _measured_quantities(self, identity: ClusterIdentity, result: SyncResult) -> None:
        """Accrue what the change measurement found besides the saving, for verified recommendations.

        * Released capacity (``cpu_core_h``, ``mem_gib_h``): the request effect
          the measurement booked (``capacity.total``, signed: an increase
          reserves capacity), split by the recommendation's allocation share,
          from ``applied_at`` on. Its own quantities, never money.
        * The cost of a reliability fix (``cost_of_fix_annual``, MEA-14): a
          negative ``cost`` entry with reason ``cost_of_fix``, never a saving.

        Both are derived from the measurement, so they sit in the
        ``restatement`` book (the ``accrued`` book mirrors the v1 staging
        ledger) and are accrued per hour slot up to the last complete slot.
        """
        records = {
            int(record.id): record
            for record in await self._records_of_pass()
            if _value(record.status) == "verified"
            and record.applied_at is not None
            and not is_non_representative(record)
        }
        if not records:
            return
        provenance = await self.repo.provenance(records)
        facts = await self.repo.change_measurement_facts(
            source.change_id for source in provenance.values() if source.change_id
        )
        ends = await self.repo.measured_quantity_ends(identity.cluster_uid, records)
        horizon = _floor_slot(self._now())
        step = timedelta(seconds=SLOT_SECONDS)
        drafts: List[LedgerEntryDraft] = []
        for rec_id, record in records.items():
            source = provenance.get(rec_id, Provenance())
            fact = facts.get(source.change_id or "")
            if not fact:
                continue
            for reason, quantity, rate, unit, currency in _measured_rates(
                record, rec_id, fact, source.currency or self.currency
            ):
                start = ends.get((rec_id, reason, quantity)) or ensure_utc(record.applied_at)
                cursor = _floor_slot(start)
                written = 0
                while cursor < horizon and written < MAX_MEASURED_SLOTS_PER_PASS:
                    begin, finish = max(cursor, start), cursor + step
                    if finish > begin:
                        prefix = "cost_of_fix" if reason == REASON_COST_OF_FIX else "capacity"
                        drafts.append(
                            LedgerEntryDraft(
                                entry_key=f"{prefix}:{rec_id}:{format_time(cursor)}:{quantity}",
                                group_key=f"{prefix}:{rec_id}:{format_time(cursor)}",
                                entry_kind=ENTRY_ACCRUAL,
                                book=BOOK_RESTATEMENT,
                                reason=reason,
                                method=METHOD_MEASURED_ALLOCATED,
                                quantity=quantity,
                                value=rate * (finish - begin).total_seconds() / SLOT_SECONDS,
                                unit=unit,
                                currency=currency,
                                method_version=source.method_version or LEDGER_WRITER_VERSION,
                                period_start=begin,
                                period_end=finish,
                                slot_start=cursor,
                                created_by="ledger-measurement",
                                recommendation_id=rec_id,
                                recommendation_ids=list(source.recommendation_ids or (rec_id,)),
                                namespace=getattr(record, "namespace", None) or "",
                                recommendation_type=_value(getattr(record, "type", "")),
                                change_id=source.change_id,
                                measurement_id=fact.get("measurement_id"),
                                pr_url=source.pr_url,
                                basis=BASIS_ALLOCATED,
                            )
                        )
                        written += 1
                    cursor += step
        if drafts:
            inserted = await self.repo.append(identity.cluster_uid, identity.cluster_name, drafts, now=self._now())
            result.restated += len(inserted)


def _measured_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _measured_rates(
    record: Any, rec_id: int, fact: Dict[str, Any], currency: str
) -> List[tuple[str, str, float, str, Optional[str]]]:
    """``(reason, quantity, rate per hour, unit, currency)`` a verified recommendation accrues from its measurement."""
    allocations = fact.get("allocations") if isinstance(fact.get("allocations"), list) else []
    allocation = next(
        (item for item in allocations if isinstance(item, dict) and item.get("recommendation_id") == rec_id), None
    )
    if allocation is None:
        return []
    share = _measured_number(allocation.get("share")) or 0.0
    component = str(allocation.get("quantity") or "")
    rates: List[tuple[str, str, float, str, Optional[str]]] = []
    capacity = fact.get("capacity") if isinstance(fact.get("capacity"), dict) else {}
    total = capacity.get("total") if isinstance(capacity.get("total"), dict) else {}
    if capacity.get("verdict") not in (None, "unknown"):
        rec_type = _value(getattr(record, "type", "")).upper()
        for quantity, key, own_type, other_component in (
            (QUANTITY_CPU_CORE_HOURS, "cpu_cores", "RIGHTSIZING_CPU", "cost_ram"),
            (QUANTITY_MEM_GIB_HOURS, "mem_gib", "RIGHTSIZING_MEMORY", "cost_cpu"),
        ):
            # A CPU change releases no memory of its own (and the reverse): the other component's share is 0.
            if component == other_component or (rec_type.startswith("RIGHTSIZING_") and rec_type != own_type):
                continue
            value = _measured_number(total.get(key))
            if value:
                rates.append((REASON_RELEASED_CAPACITY, quantity, value * share, CAPACITY_UNITS[quantity], None))
    params = fact.get("counterfactual_params") if isinstance(fact.get("counterfactual_params"), dict) else {}
    cost_of_fix = _measured_number(params.get("cost_of_fix_annual"))
    if params.get("objective") == "reliability" and cost_of_fix:
        hours_per_year = SECONDS_PER_YEAR / SLOT_SECONDS
        rates.append((REASON_COST_OF_FIX, QUANTITY_COST, -cost_of_fix * share / hours_per_year, currency, currency))
    return rates


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
        basis=entry.effective_basis,
    )


def restatement_of(projection: LedgerEntry, *, annual_rate: float, provenance: Provenance) -> List[LedgerEntryDraft]:
    """The reversal of a projected accrual and the measured amount of its period, linked to it."""
    seconds = (ensure_utc(projection.period_end) - ensure_utc(projection.period_start)).total_seconds()
    reversal = reversal_of(projection, REASON_RESTATEMENT, created_by="ledger-restatement")
    reversal = replace(
        reversal,
        book=BOOK_RESTATEMENT,
        change_id=provenance.change_id or projection.change_id,
        measurement_id=provenance.measurement_id,
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
        basis=BASIS_ALLOCATED,
        annual_rate=float(annual_rate),
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

            return await get_recommendation_repository().get_recommendations_by_statuses(list(PROJECTOR_STATUSES))

    resolver = ClusterIdentityResolver(repo, cluster_name or savings_cluster_name(), reader=reader)
    currency = str(getattr(get_config(), "IMPACT_PRICING_CURRENCY", "USD") or "USD")
    return LedgerProjector(repo, resolver, records=records, currency=currency)


__all__ = [
    "LEDGER_LATE_ROWS",
    "LEDGER_UNPROJECTED_ROWS",
    "LEDGER_WRITE_ERRORS",
    "LedgerProjector",
    "SyncResult",
    "build_projector",
    "restatement_of",
    "reversal_of",
]
