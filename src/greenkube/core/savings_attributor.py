# src/greenkube/core/savings_attributor.py
"""
Savings attribution service.

Turns the annual savings rate of each applied recommendation into accrual rows
on a fixed UTC grid of one-hour slots, stored in the append-only
``recommendation_savings_ledger``:

    accrual = annual_rate × seconds_of_slot_after_apply / seconds_per_year

Accrual is idempotent. A per-cluster watermark in ``accrual_watermarks``
records the end of the last accrued slot; each run accrues every complete slot
after it (backfilling downtime) and advances it with a compare-and-swap in the
same transaction as the inserts. Rows are keyed by recommendation, method and
slot, so restarts, retries and concurrent controllers cannot double count.
A recommendation that has never accrued is also caught up for the slots since
its ``applied_at`` that are already behind the watermark (bounded by the
initial backfill), so a change detected late keeps its full history.

Which rate accrues, and under which method, depends on the recommendation's
outcome (roadmap C0.4):

- ``verified`` with measured values: ``measured``;
- ``applied``/``verifying`` while verification is pending: ``projected``, or
  ``user_declared`` for the quantities a user declared on ``PATCH /apply``;
- ``not_realized``, ``inconclusive``, ``failed``, ``rollback_review``,
  ``reverted`` and every other state: nothing.

Each recommendation is processed in isolation: an invalid record increments
``greenkube_attribution_errors_total`` and never blocks the others.

Dual write (roadmap C6): when a ledger projector is given, every run that
reached the database is followed by a pass of the append-only, hash-chained
savings ledger v2 (``savings_ledger_entries``), which projects the rows just
written. The v1 ledger stays the source of the legacy views; a failed v2 pass
is counted (``greenkube_ledger_write_errors_total``) and caught up by the
``ledger-chain`` controller loop, never failing the accrual.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite
from typing import Any, Callable, Iterable, List, Optional

from prometheus_client import Counter

from ..models.metrics import RecommendationRecord
from ..models.savings import (
    ACCRUAL_SLOT_SECONDS,
    METHOD_MEASURED,
    METHOD_PROJECTED,
    METHOD_USER_DECLARED,
    SavingsLedgerRecord,
    UserDeclaredSavings,
    floor_to_slot,
)
from ..storage.base_savings_repository import ATTRIBUTION_STREAM, AccrualWatermarkConflict, SavingsLedgerRepository
from ..utils.date_utils import ensure_utc
from ..verify.ledger.model import BASIS_ALLOCATED, BASIS_BILLABLE_EXPECTED, BASIS_DECLARED
from .observability import REGISTRY as OBSERVABILITY_REGISTRY

logger = logging.getLogger(__name__)

# Seconds in a year — basis for the accrual formula.
_SECONDS_PER_YEAR: float = 365.25 * 24 * 3600

# Without a watermark (first run after install or upgrade), backfill this far.
INITIAL_BACKFILL = timedelta(days=7)
# Upper bound of slots accrued by one run; a long outage catches up over runs.
MAX_SLOTS_PER_RUN = 7 * 24
# A user-declared saving may not exceed this multiple of the projection.
USER_DECLARED_MAX_RATIO = 10.0

_ACCRUING_STATUSES = frozenset({"applied", "verifying"})
_MEASURED_STATUSES = frozenset({"verified"})
_PENDING_VERIFICATION = frozenset({"", "pending", "in_progress"})

ATTRIBUTION_ERRORS = Counter(
    "greenkube_attribution_errors_total",
    "Savings attribution failures, by reason. One invalid recommendation never blocks the others.",
    ["reason"],
    registry=OBSERVABILITY_REGISTRY,
)


class InvalidSavingsRecord(ValueError):
    """A recommendation carries savings that cannot be accrued."""


def _value(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw.value if hasattr(raw, "value") else raw)


def savings_cluster_name() -> str:
    """Cluster key used by the attribution job and every ledger reader."""
    from .config import get_config

    return get_config().CLUSTER_NAME or "default"


@dataclass(frozen=True)
class AccrualComponent:
    """An annual savings rate accrued under one method."""

    method: str
    co2e_grams_per_year: float
    cost_per_year: float
    #: Accounting basis (LED-01): ``allocated`` for projections and measurements, ``declared`` for
    #: user declarations, ``billable_expected`` only for projections without an Impact Model projection.
    basis: str = BASIS_ALLOCATED

    @property
    def is_zero(self) -> bool:
        return not self.co2e_grams_per_year and not self.cost_per_year


@dataclass(frozen=True)
class AccrualResult:
    """Outcome of one attribution run."""

    slots: int = 0
    written: int = 0
    errors: int = 0
    watermark: Optional[datetime] = None
    conflict: bool = False
    fenced: bool = False


def _finite(label: str, value: Optional[float], record_id: Optional[int]) -> float:
    if value is None:
        return 0.0
    number = float(value)
    if not isfinite(number):
        raise InvalidSavingsRecord(f"recommendation {record_id}: {label} is not a finite number ({value!r})")
    return number


def user_declared_savings(record: RecommendationRecord) -> Optional[UserDeclaredSavings]:
    """Return the validated user declaration stored on the record, if any."""
    baseline = record.baseline
    if baseline is None:
        return None
    raw = baseline.get("user_declared_savings") if hasattr(baseline, "get") else None
    if not raw:
        return None
    return UserDeclaredSavings.model_validate(raw)


def _impact(record: RecommendationRecord) -> Optional[dict]:
    impact = record.impact
    if impact is None:
        return None
    if hasattr(impact, "model_dump"):
        impact = impact.model_dump()
    return impact if isinstance(impact, dict) else None


def _impact_number(impact: Optional[dict], key: str) -> Optional[float]:
    value = impact.get(key) if impact is not None else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        return None
    return float(value)


def projected_allocated_rates(record: RecommendationRecord) -> Optional[tuple[float, float]]:
    """The projection's annual (cost, CO2e grams) on the ALLOCATED basis, or None without an Impact Model one.

    The same quantities a change measurement compares with
    (:func:`greenkube.verify.attribution.allocated_projection` and the
    allocated CO2e of the measured components), so that ``projected`` and
    ``measured_allocated`` accruals are one quantity before and after
    measurement (LED-01). The realization factor is never applied: the
    billable expectation is reported apart (``realization``) and never accrued.
    A projection that does not model CO2e accrues no CO2e. A rightsizing
    applied more conservatively than recommended is scaled by the share of the
    cut it realizes, like its billable value (IMP-06).
    """
    impact = _impact(record)
    cost = _impact_number(impact, "allocated_cost_annual")
    if cost is None:
        return None
    from ..impact.comparison import measurable_co2e_projection
    from ..impact.types import MEASURED_CO2E_COMPONENTS
    from .recommendation_realization import applied_share

    co2e = measurable_co2e_projection(impact, MEASURED_CO2E_COMPONENTS, allocated=True)
    co2e = float(co2e) if co2e is not None and isfinite(float(co2e)) else 0.0
    share = applied_share(record)
    if share is not None:
        cost, co2e = cost * share, co2e * share
    return cost, co2e


def _projection_components(record: RecommendationRecord) -> List[AccrualComponent]:
    allocated = projected_allocated_rates(record)
    if allocated is not None:
        projected_cost, projected_co2e = allocated
        basis = BASIS_ALLOCATED
    else:
        # Recommendations stored without an Impact Model projection: the legacy value, labelled as such.
        projected_co2e = _finite("carbon_saved_co2e_grams", record.carbon_saved_co2e_grams, record.id)
        projected_cost = _finite("cost_saved", record.cost_saved, record.id)
        basis = BASIS_BILLABLE_EXPECTED
    components: List[AccrualComponent] = []
    declared = user_declared_savings(record)
    if declared is not None:
        declared_co2e = _finite("user_declared co2e", declared.co2e_grams_per_year, record.id)
        declared_cost = _finite("user_declared cost", declared.cost_per_year, record.id)
        components.append(AccrualComponent(METHOD_USER_DECLARED, declared_co2e, declared_cost, BASIS_DECLARED))
        if declared.co2e_grams_per_year is not None:
            projected_co2e = 0.0
        if declared.cost_per_year is not None:
            projected_cost = 0.0
    components.append(AccrualComponent(METHOD_PROJECTED, projected_co2e, projected_cost, basis))
    return [component for component in components if not component.is_zero]


#: Recommendation baseline key set to ``False`` by an accelerated demo measurement (MEA-10).
REPRESENTATIVE_KEY = "measurement_representative"


def is_non_representative(record: RecommendationRecord) -> bool:
    """Whether the record's latest measurement is a demo verdict (``representative: false``).

    Accelerated demo windows decide on demo thresholds: their measured saving
    never accrues in the ledger and never restates a projection.
    """
    baseline = record.baseline
    if baseline is None:
        return False
    values = baseline.model_dump() if hasattr(baseline, "model_dump") else dict(baseline)
    return values.get(REPRESENTATIVE_KEY) is False


def accrual_components(record: RecommendationRecord, at: Optional[datetime] = None) -> List[AccrualComponent]:
    """Return the annual rates a recommendation accrues, by method.

    ``at`` is the end of the slot being accrued: a verified recommendation
    accrues its projection for slots that ended before ``verified_at`` (this
    only matters when backfilling) and its measured savings afterwards.
    """
    status = _value(record.status)
    if status in _MEASURED_STATUSES:
        verified_at = record.verified_at
        if at is not None and verified_at is not None and at <= ensure_utc(verified_at):
            return _projection_components(record)
        if record.measured_co2e_saved_grams is None and record.measured_cost_saved is None:
            return []
        if is_non_representative(record):
            # A demo verdict (accelerated windows) is not a measured saving: nothing accrues.
            return []
        measured = AccrualComponent(
            METHOD_MEASURED,
            _finite("measured_co2e_saved_grams", record.measured_co2e_saved_grams, record.id),
            _finite("measured_cost_saved", record.measured_cost_saved, record.id),
        )
        return [] if measured.is_zero else [measured]
    if status not in _ACCRUING_STATUSES:
        return []
    if _value(record.verification_status) not in _PENDING_VERIFICATION:
        # failed (not realized), inconclusive or any later outcome: no accrual.
        return []
    return _projection_components(record)


def annual_rates_by_method(records: Iterable[RecommendationRecord]) -> dict[str, dict[str, float]]:
    """Sum the current annual rates of ``records`` per method.

    Returns ``{method: {"co2e_grams": float, "cost": float, "count": int}}``.
    Records with invalid values are skipped; the attribution job reports them
    through ``greenkube_attribution_errors_total``.
    """
    totals: dict[str, dict[str, float]] = {}
    for record in records:
        try:
            components = accrual_components(record)
        except (InvalidSavingsRecord, ValueError) as exc:
            logger.debug("Skipping recommendation %s in savings rates: %s", record.id, exc)
            continue
        for component in components:
            bucket = totals.setdefault(component.method, {"co2e_grams": 0.0, "cost": 0.0, "count": 0})
            bucket["co2e_grams"] += component.co2e_grams_per_year
            bucket["cost"] += component.cost_per_year
            bucket["count"] += 1
    return totals


def validate_user_declared_savings(
    record: RecommendationRecord,
    *,
    co2e_grams_per_year: Optional[float],
    cost_per_year: Optional[float],
) -> Optional[UserDeclaredSavings]:
    """Validate savings a user declares on ``PATCH /apply`` (roadmap C0.8).

    Values must be finite and at most ten times the recommendation's projection
    in magnitude. The projection is the allocated one when the record has an
    Impact Model projection (IMP-06): a rightsizing on fixed nodes, whose
    billable projection is 0, still accepts a declaration. Negative values are
    accepted and flagged. Returns ``None`` when nothing was declared.

    Raises:
        ValueError: with a user-facing explanation when a value is rejected.
    """
    if co2e_grams_per_year is None and cost_per_year is None:
        return None
    flags: List[str] = []
    allocated = projected_allocated_rates(record)

    def bound(allocated_value: Optional[float], billable: Optional[float]) -> Optional[float]:
        # Per quantity: the allocated projection when it has a value, else the billable one (an
        # allocated projection that models no CO2e, or an apply that realized none of the cut).
        return allocated_value if allocated_value else billable

    projected_cost = bound(allocated[0] if allocated else None, record.potential_savings_cost)
    projected_co2e = bound(allocated[1] if allocated else None, record.potential_savings_co2e_grams)
    checks = (
        ("carbon_saved_co2e_grams", co2e_grams_per_year, projected_co2e),
        ("cost_saved", cost_per_year, projected_cost),
    )
    for label, value, projection in checks:
        if value is None:
            continue
        if not isfinite(value):
            raise ValueError(f"{label} must be a finite number.")
        if value < 0:
            flags.append(f"negative_{label}")
        if value == 0:
            continue
        if projection is None or not isfinite(projection) or projection == 0:
            raise ValueError(
                f"{label} cannot be accepted: the recommendation has no projected {label} to bound a declared value."
            )
        bound = USER_DECLARED_MAX_RATIO * abs(projection)
        if abs(value) > bound:
            raise ValueError(
                f"{label}={value:g} exceeds {USER_DECLARED_MAX_RATIO:g}x the projected value ({projection:g})."
            )
    if flags:
        logger.warning("Recommendation %s: user-declared savings flagged %s.", record.id, ", ".join(flags))
    return UserDeclaredSavings(
        co2e_grams_per_year=co2e_grams_per_year,
        cost_per_year=cost_per_year,
        projected_co2e_grams_per_year=projected_co2e,
        projected_cost_per_year=projected_cost,
        flags=flags,
    )


async def record_user_declared_savings(
    repo: Any,
    record: RecommendationRecord,
    declared: UserDeclaredSavings,
) -> RecommendationRecord:
    """Store a validated declaration on the record's baseline (``user_declared_savings``)."""
    if record.id is None:
        return record
    baseline = record.baseline
    if baseline is None:
        payload: dict = {}
    elif hasattr(baseline, "model_dump"):
        payload = baseline.model_dump(mode="json")
    else:
        payload = dict(baseline)
    payload["user_declared_savings"] = declared.model_dump(mode="json")
    return await repo.update_recommendation_fields(record.id, {"baseline": payload})


class SavingsAttributor:
    """Accrues savings of applied recommendations into the ledger."""

    def __init__(
        self,
        savings_repo: SavingsLedgerRepository,
        cluster_name: str,
        *,
        stream: str = ATTRIBUTION_STREAM,
        fence: Optional[Callable[[], bool]] = None,
        initial_backfill: timedelta = INITIAL_BACKFILL,
        max_slots_per_run: int = MAX_SLOTS_PER_RUN,
        ledger: Any = None,
    ) -> None:
        self._repo = savings_repo
        #: Optional :class:`~greenkube.verify.ledger.projector.LedgerProjector` (dual write, C6).
        self._ledger = ledger
        self._cluster = cluster_name
        self._stream = stream
        self._fence = fence
        self._initial_backfill = initial_backfill
        self._max_slots = max(1, int(max_slots_per_run))
        self._slot = timedelta(seconds=ACCRUAL_SLOT_SECONDS)

    def _slots(self, start: datetime, now: datetime) -> List[tuple[datetime, datetime]]:
        horizon = floor_to_slot(now)
        slots: List[tuple[datetime, datetime]] = []
        cursor = floor_to_slot(start)
        while cursor < horizon and len(slots) < self._max_slots:
            slots.append((cursor, cursor + self._slot))
            cursor += self._slot
        return slots

    def _accruals_for(
        self,
        record: RecommendationRecord,
        slots: List[tuple[datetime, datetime]],
        floor: Optional[datetime],
    ) -> List[SavingsLedgerRecord]:
        assert record.id is not None
        status = _value(record.status)
        if status not in _ACCRUING_STATUSES and status not in _MEASURED_STATUSES:
            return []
        current = accrual_components(record)
        verified_at = ensure_utc(record.verified_at) if record.verified_at is not None else None
        before_verification = (
            _projection_components(record) if status in _MEASURED_STATUSES and verified_at is not None else []
        )
        if not current and not before_verification:
            return []
        if record.applied_at is None:
            raise InvalidSavingsRecord(f"recommendation {record.id} is applied but has no applied_at")
        accrue_from = ensure_utc(record.applied_at)
        if floor is not None and floor > accrue_from:
            accrue_from = floor
        # A verified record accrues its projection until verified_at and its
        # measured savings afterwards; the slot holding verified_at is split.
        split_at = verified_at if status in _MEASURED_STATUSES else None
        rec_type = _value(record.type)
        rows: List[SavingsLedgerRecord] = []
        for slot_start, slot_end in slots:
            begin = max(slot_start, accrue_from)
            if begin >= slot_end:
                continue
            if split_at is None:
                segments = [(begin, current)]
            else:
                segments = []
                if begin < split_at:
                    segments.append((begin, before_verification))
                if split_at < slot_end:
                    segments.append((max(begin, split_at), current))
            for segment_index, (segment_start, components) in enumerate(segments):
                segment_end = segments[segment_index + 1][0] if segment_index + 1 < len(segments) else slot_end
                seconds = (segment_end - segment_start).total_seconds()
                if seconds <= 0:
                    continue
                factor = seconds / _SECONDS_PER_YEAR
                for component in components:
                    rows.append(
                        SavingsLedgerRecord(
                            recommendation_id=record.id,
                            cluster_name=self._cluster,
                            namespace=record.namespace or "",
                            recommendation_type=rec_type,
                            co2e_saved_grams=component.co2e_grams_per_year * factor,
                            cost_saved_dollars=component.cost_per_year * factor,
                            period_seconds=max(1, ceil(seconds)),
                            timestamp=segment_end,
                            period_start=segment_start,
                            period_end=segment_end,
                            slot_start=slot_start,
                            measurement_method=component.method,
                            baseline_value=record.potential_savings_co2e_grams,
                            actual_value=component.co2e_grams_per_year,
                            confidence=record.confidence if component.method == METHOD_PROJECTED else None,
                            basis=component.basis,
                        )
                    )
        return rows

    def _catch_up_slots(
        self,
        record: RecommendationRecord,
        first_slot_start: datetime,
        lower_bound: datetime,
    ) -> List[tuple[datetime, datetime]]:
        """Return the slots before this run that a never-accrued record missed.

        The watermark is global, so a record that became applied after its
        ``applied_at`` (late detection, rollout-completion time, downtime) would
        otherwise lose every slot between ``applied_at`` and the watermark.
        The ``(recommendation_id, method, slot_start)`` key keeps this safe.
        """
        if record.applied_at is None:
            return []
        cursor = floor_to_slot(max(ensure_utc(record.applied_at), lower_bound))
        slots: List[tuple[datetime, datetime]] = []
        while cursor < first_slot_start:
            slots.append((cursor, cursor + self._slot))
            cursor += self._slot
        return slots

    async def accrue(
        self,
        applied_records: List[RecommendationRecord],
        *,
        now: Optional[datetime] = None,
    ) -> AccrualResult:
        """Accrue every complete slot since the watermark (bounded per run).

        Never raises for a single bad record; persistence failures are logged,
        counted and reported in the result.
        """
        current = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        watermark = await self._repo.get_accrual_watermark(self._cluster, self._stream)
        start = watermark if watermark is not None else floor_to_slot(current) - self._initial_backfill
        slots = self._slots(start, current)
        if not slots:
            return AccrualResult(watermark=watermark)
        new_watermark = slots[-1][1]

        candidates = [record for record in applied_records if record.id is not None]
        floors = await self._repo.get_accrual_floors([record.id for record in candidates if record.id is not None])

        # Records that never accrued are caught up for slots already behind the
        # watermark, within the same horizon as a first run.
        catch_up_from = slots[0][0] - self._initial_backfill if watermark is not None else None

        rows: List[SavingsLedgerRecord] = []
        errors = 0
        for record in candidates:
            try:
                floor = floors.get(record.id or 0)
                record_slots = slots
                if floor is None and catch_up_from is not None:
                    record_slots = self._catch_up_slots(record, slots[0][0], catch_up_from) + slots
                rows.extend(self._accruals_for(record, record_slots, floor))
            except Exception as exc:
                errors += 1
                ATTRIBUTION_ERRORS.labels(reason="invalid_record").inc()
                logger.warning("Savings attribution skipped recommendation %s: %s", record.id, exc)

        if self._fence is not None and not self._fence():
            logger.info("Savings attribution stopped before writing: this process no longer holds the lease.")
            return AccrualResult(slots=len(slots), errors=errors, watermark=watermark, fenced=True)

        try:
            written = await self._repo.append_accruals(
                rows,
                cluster_name=self._cluster,
                stream=self._stream,
                expected_watermark=watermark,
                new_watermark=new_watermark,
            )
        except AccrualWatermarkConflict as exc:
            logger.info("Savings attribution skipped: %s", exc)
            return AccrualResult(slots=len(slots), errors=errors, watermark=watermark, conflict=True)
        except Exception as exc:
            ATTRIBUTION_ERRORS.labels(reason="persist_failed").inc()
            logger.error("Savings attribution could not persist %d accrual row(s): %s", len(rows), exc)
            return AccrualResult(slots=len(slots), errors=errors + 1, watermark=watermark)

        await self._sync_ledger()
        return AccrualResult(slots=len(slots), written=written, errors=errors, watermark=new_watermark)

    async def _sync_ledger(self) -> None:
        """Project the rows just written into the savings ledger v2 (never fails the accrual)."""
        if self._ledger is None:
            return
        try:
            await self._ledger.sync()
        except Exception as exc:
            from ..verify.ledger.projector import LEDGER_WRITE_ERRORS

            LEDGER_WRITE_ERRORS.labels(reason="dual_write_failed").inc()
            logger.error("Savings ledger v2 dual write failed (caught up by the ledger-chain loop): %s", exc)
