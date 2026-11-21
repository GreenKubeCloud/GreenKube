# tests/core/test_savings_attributor.py
"""Tests for SavingsAttributor — idempotent accrual of savings on a fixed UTC grid.

Covers roadmap C0.4 (BUG-30), C0.7 (BUG-33) and C0.8 (BUG-34).
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import pytest

from greenkube.core.savings_attributor import (
    _SECONDS_PER_YEAR,
    ATTRIBUTION_ERRORS,
    SavingsAttributor,
    accrual_components,
    annual_rates_by_method,
    record_user_declared_savings,
    user_declared_savings,
    validate_user_declared_savings,
)
from greenkube.models.metrics import RecommendationBaseline, RecommendationRecord, RecommendationStatus
from greenkube.models.metrics import RecommendationType as RT
from greenkube.models.savings import SavingsAccrualTotal, SavingsCounterTotal, SavingsLedgerRecord, UserDeclaredSavings
from greenkube.storage.base_savings_repository import AccrualWatermarkConflict, SavingsLedgerRepository

NOW = datetime(2026, 10, 1, 12, 7, tzinfo=timezone.utc)
HOURLY_RATE = _SECONDS_PER_YEAR / 3600  # an annual rate that accrues exactly 1 per hour


class FakeLedger(SavingsLedgerRepository):
    """In-memory ledger with the same idempotency key and watermark CAS as the SQL adapters."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, SavingsLedgerRecord] = {}
        self.watermarks: Dict[tuple, datetime] = {}
        self.fail_append = False

    async def save_records(self, records: List[SavingsLedgerRecord]) -> int:
        inserted = 0
        for record in records:
            key = (record.recommendation_id, record.measurement_method, record.slot_start or record.period_start)
            if key not in self.rows:
                self.rows[key] = record
                inserted += 1
        return inserted

    async def append_accruals(self, records, *, cluster_name, stream, expected_watermark, new_watermark) -> int:
        if self.fail_append:
            raise RuntimeError("database unavailable")
        if self.watermarks.get((cluster_name, stream)) != expected_watermark:
            raise AccrualWatermarkConflict("moved")
        self.watermarks[(cluster_name, stream)] = new_watermark
        return await self.save_records(records)

    async def supersede_for_recommendation(self, recommendation_id: int) -> int:
        return 0

    async def get_accrual_watermark(self, cluster_name: str, stream: str = "recommendation_savings"):
        return self.watermarks.get((cluster_name, stream))

    async def get_accrual_floors(self, recommendation_ids: Iterable[int]) -> Dict[int, datetime]:
        floors: Dict[int, datetime] = {}
        for record in self.rows.values():
            if record.recommendation_id in set(recommendation_ids) and record.period_end is not None:
                current = floors.get(record.recommendation_id)
                floors[record.recommendation_id] = max(current, record.period_end) if current else record.period_end
        return floors

    async def get_accrual_breakdown(self, cluster_name, start_time=None, end_time=None, namespace=None):
        totals: Dict[tuple, SavingsAccrualTotal] = {}
        for record in self.rows.values():
            key = (record.namespace, record.recommendation_type, record.measurement_method)
            total = totals.setdefault(
                key, SavingsAccrualTotal(namespace=key[0], recommendation_type=key[1], method=key[2])
            )
            total.co2e_saved_grams += record.co2e_saved_grams
            total.cost_saved_dollars += record.cost_saved_dollars
        return list(totals.values())

    async def get_lifetime_counters(self, cluster_name: str) -> List[SavingsCounterTotal]:
        return []

    async def has_legacy_compressed_rows(self, cluster_name, start_time=None, end_time=None, namespace=None) -> bool:
        return False

    def method_sum(self, method: str, rec_id: Optional[int] = None) -> float:
        return sum(
            r.co2e_saved_grams
            for r in self.rows.values()
            if r.measurement_method == method and (rec_id is None or r.recommendation_id == rec_id)
        )


def _record(
    rec_id: int = 1,
    *,
    status: RecommendationStatus = RecommendationStatus.APPLIED,
    verification_status: Optional[str] = "pending",
    applied_at: Optional[datetime] = NOW - timedelta(hours=3),
    co2e: Optional[float] = HOURLY_RATE,
    cost: Optional[float] = HOURLY_RATE / 100,
    **updates,
) -> RecommendationRecord:
    return RecommendationRecord(
        id=rec_id,
        pod_name=f"pod-{rec_id}",
        namespace="prod",
        type=RT.RIGHTSIZING_CPU,
        description="cut cpu",
        status=status,
        verification_status=verification_status,
        applied_at=applied_at,
        carbon_saved_co2e_grams=co2e,
        cost_saved=cost,
        potential_savings_co2e_grams=co2e,
        potential_savings_cost=cost,
        confidence=0.8,
        **updates,
    )


def _errors(reason: str = "invalid_record") -> float:
    return ATTRIBUTION_ERRORS.labels(reason=reason)._value.get()


class TestAccrualEligibility:
    """C0.4 / BUG-30: only pending and verified outcomes accrue."""

    def test_applied_pending_accrues_projected(self):
        [component] = accrual_components(_record())
        assert component.method == "projected"
        assert component.co2e_grams_per_year == pytest.approx(HOURLY_RATE)

    def test_verified_accrues_measured_values(self):
        record = _record(
            status=RecommendationStatus.VERIFIED,
            verification_status="passed",
            measured_co2e_saved_grams=90.0,
            measured_cost_saved=7.5,
        )
        [component] = accrual_components(record)
        assert (component.method, component.co2e_grams_per_year, component.cost_per_year) == ("measured", 90.0, 7.5)

    @pytest.mark.parametrize(
        ("status", "verification_status"),
        [
            (RecommendationStatus.APPLIED, "failed"),  # not_realized
            (RecommendationStatus.APPLIED, "inconclusive"),
            (RecommendationStatus.APPLIED, "not_realized"),
            (RecommendationStatus.FAILED, "failed"),
            (RecommendationStatus.ROLLBACK_REVIEW, "failed"),
            (RecommendationStatus.REVERTED, "passed"),
            (RecommendationStatus.VERIFIED, "passed"),  # verified without measured values
        ],
    )
    def test_failed_outcomes_accrue_nothing(self, status, verification_status):
        assert accrual_components(_record(status=status, verification_status=verification_status)) == []

    def test_user_declared_quantities_are_labelled_user_declared(self):
        record = _record(
            baseline=RecommendationBaseline(
                user_declared_savings=UserDeclaredSavings(cost_per_year=50.0).model_dump(mode="json")
            )
        )
        components = {c.method: c for c in accrual_components(record)}
        assert components["user_declared"].cost_per_year == 50.0
        assert components["user_declared"].co2e_grams_per_year == 0.0
        assert components["projected"].cost_per_year == 0.0
        assert components["projected"].co2e_grams_per_year == pytest.approx(HOURLY_RATE)

    def test_annual_rates_never_mix_methods(self):
        records = [
            _record(1),
            _record(
                2,
                status=RecommendationStatus.VERIFIED,
                verification_status="passed",
                measured_co2e_saved_grams=10.0,
                measured_cost_saved=1.0,
            ),
            _record(3, status=RecommendationStatus.ROLLBACK_REVIEW),
        ]
        rates = annual_rates_by_method(records)
        assert set(rates) == {"projected", "measured"}
        assert rates["projected"]["count"] == 1
        assert rates["measured"]["co2e_grams"] == 10.0


class TestAccrual:
    @pytest.mark.asyncio
    async def test_accrues_complete_slots_with_partial_first_slot(self):
        ledger = FakeLedger()
        result = await SavingsAttributor(ledger, "c").accrue([_record()], now=NOW)

        assert result.written == 3
        assert result.watermark == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        slots = sorted(r.slot_start for r in ledger.rows.values() if r.slot_start is not None)
        assert slots == [NOW.replace(minute=0) - timedelta(hours=h) for h in (3, 2, 1)]
        first = min(ledger.rows.values(), key=lambda r: r.period_start or NOW)
        assert first.period_start == NOW - timedelta(hours=3)
        assert first.co2e_saved_grams == pytest.approx(53 / 60)
        assert ledger.method_sum("projected") == pytest.approx(53 / 60 + 2)

    @pytest.mark.asyncio
    async def test_second_run_writes_nothing(self):
        """BUG-33: a restart or retry in the same slot never double counts."""
        ledger = FakeLedger()
        attributor = SavingsAttributor(ledger, "c")
        await attributor.accrue([_record()], now=NOW)
        again = await attributor.accrue([_record()], now=NOW + timedelta(seconds=2))
        assert again.slots == 0 and again.written == 0
        assert len(ledger.rows) == 3

    @pytest.mark.asyncio
    async def test_backfill_is_bounded_per_run_and_catches_up(self):
        ledger = FakeLedger()
        record = _record(applied_at=NOW - timedelta(days=30))
        attributor = SavingsAttributor(ledger, "c", max_slots_per_run=24, initial_backfill=timedelta(days=2))

        first = await attributor.accrue([record], now=NOW)
        second = await attributor.accrue([record], now=NOW)
        third = await attributor.accrue([record], now=NOW)

        assert (first.slots, second.slots, third.slots) == (24, 24, 0)
        assert len(ledger.rows) == 48
        assert ledger.method_sum("projected") == pytest.approx(48.0)

    @pytest.mark.asyncio
    async def test_verified_backfill_keeps_projection_before_verification(self):
        ledger = FakeLedger()
        record = _record(
            status=RecommendationStatus.VERIFIED,
            verification_status="passed",
            applied_at=NOW - timedelta(hours=4),
            verified_at=NOW - timedelta(hours=2),
            measured_co2e_saved_grams=2 * HOURLY_RATE,
            measured_cost_saved=0.0,
        )
        await SavingsAttributor(ledger, "c").accrue([record], now=NOW)
        # 08:07-10:07 projected (1/h), 10:07-12:00 measured (2/h): the slot that
        # holds verified_at is split pro rata between the two methods.
        assert ledger.method_sum("projected") == pytest.approx(2.0)
        assert ledger.method_sum("measured") == pytest.approx(2 * (53 / 60 + 1))
        split_slot = NOW.replace(minute=0) - timedelta(hours=2)
        in_split_slot = {r.measurement_method: r for r in ledger.rows.values() if r.slot_start == split_slot}
        assert in_split_slot["projected"].period_end == record.verified_at
        assert in_split_slot["measured"].period_start == record.verified_at
        assert in_split_slot["measured"].period_end == split_slot + timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_late_detected_apply_is_caught_up_behind_the_watermark(self):
        """A change detected after its applied_at keeps the slots already behind the watermark."""
        ledger = FakeLedger()
        attributor = SavingsAttributor(ledger, "c")
        await attributor.accrue([], now=NOW)
        assert ledger.watermarks[("c", "recommendation_savings")] == NOW.replace(minute=0)

        late = _record(applied_at=NOW - timedelta(hours=5))
        result = await attributor.accrue([late], now=NOW + timedelta(hours=1))

        assert result.slots == 1
        assert ledger.method_sum("projected") == pytest.approx(53 / 60 + 5)
        again = await attributor.accrue([late], now=NOW + timedelta(hours=2))
        assert again.written == 1
        assert ledger.method_sum("projected") == pytest.approx(53 / 60 + 6)

    @pytest.mark.asyncio
    async def test_catch_up_is_bounded_and_skips_records_that_already_accrued(self):
        ledger = FakeLedger()
        attributor = SavingsAttributor(ledger, "c", initial_backfill=timedelta(hours=2))
        accrued = _record(1, applied_at=NOW - timedelta(hours=1))
        await attributor.accrue([accrued], now=NOW)
        assert ledger.method_sum("projected", rec_id=1) == pytest.approx(53 / 60)

        # Record 1 already has ledger rows: no gap is re-accrued, only the new slot.
        # Record 2 never accrued: caught up, but no further back than the backfill horizon.
        old = _record(2, applied_at=NOW - timedelta(days=3))
        await attributor.accrue([accrued, old], now=NOW + timedelta(hours=1))

        assert ledger.method_sum("projected", rec_id=1) == pytest.approx(53 / 60 + 1)
        assert ledger.method_sum("projected", rec_id=2) == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_invalid_record_does_not_block_others(self):
        """BUG-34: one invalid record is skipped and counted; the others accrue."""
        ledger = FakeLedger()
        before = _errors()
        records = [
            _record(1, co2e=float("nan")),
            _record(2),
            _record(3, applied_at=None),
            _record(4, co2e=-HOURLY_RATE, cost=0.0),
        ]
        result = await SavingsAttributor(ledger, "c").accrue(records, now=NOW)

        assert result.errors == 2
        assert _errors() == before + 2
        assert ledger.method_sum("projected", rec_id=2) == pytest.approx(53 / 60 + 2)
        # Signed values are recorded as such, never floored at zero.
        assert ledger.method_sum("projected", rec_id=4) == pytest.approx(-(53 / 60 + 2))

    @pytest.mark.asyncio
    async def test_watermark_conflict_writes_nothing(self):
        ledger = FakeLedger()
        attributor = SavingsAttributor(ledger, "c")

        async def moved(*args, **kwargs):
            raise AccrualWatermarkConflict("moved")

        ledger.append_accruals = moved  # type: ignore[method-assign]
        result = await attributor.accrue([_record()], now=NOW)
        assert result.conflict and result.written == 0 and not ledger.rows

    @pytest.mark.asyncio
    async def test_fence_stops_before_writing(self):
        ledger = FakeLedger()
        result = await SavingsAttributor(ledger, "c", fence=lambda: False).accrue([_record()], now=NOW)
        assert result.fenced and not ledger.rows and not ledger.watermarks

    @pytest.mark.asyncio
    async def test_persist_failure_is_counted_not_raised(self):
        ledger = FakeLedger()
        ledger.fail_append = True
        before = _errors("persist_failed")
        result = await SavingsAttributor(ledger, "c").accrue([_record()], now=NOW)
        assert result.written == 0 and result.errors == 1
        assert _errors("persist_failed") == before + 1

    @pytest.mark.asyncio
    async def test_ineligible_records_accrue_nothing(self):
        ledger = FakeLedger()
        records = [
            _record(1, verification_status="inconclusive"),
            _record(2, verification_status="failed"),
            _record(3, status=RecommendationStatus.ROLLBACK_REVIEW),
        ]
        result = await SavingsAttributor(ledger, "c").accrue(records, now=NOW)
        assert result.written == 0 and result.errors == 0


class TestUserDeclaredSavings:
    """C0.8: user-declared savings are validated and never presented as measured."""

    def test_accepts_values_within_bound(self):
        declared = validate_user_declared_savings(_record(), co2e_grams_per_year=HOURLY_RATE * 2, cost_per_year=None)
        assert declared is not None
        assert declared.co2e_grams_per_year == HOURLY_RATE * 2
        assert declared.flags == []

    def test_returns_none_when_nothing_declared(self):
        assert validate_user_declared_savings(_record(), co2e_grams_per_year=None, cost_per_year=None) is None

    def test_negative_values_are_flagged(self):
        declared = validate_user_declared_savings(_record(), co2e_grams_per_year=None, cost_per_year=-5.0)
        assert declared is not None
        assert declared.flags == ["negative_cost_saved"]

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
    def test_rejects_non_finite(self, value):
        with pytest.raises(ValueError, match="finite"):
            validate_user_declared_savings(_record(), co2e_grams_per_year=value, cost_per_year=None)

    def test_rejects_values_above_ten_times_projection(self):
        with pytest.raises(ValueError, match="exceeds 10x"):
            validate_user_declared_savings(_record(cost=10.0), co2e_grams_per_year=None, cost_per_year=101.0)

    def test_rejects_values_without_projection(self):
        with pytest.raises(ValueError, match="no projected"):
            validate_user_declared_savings(_record(cost=None), co2e_grams_per_year=None, cost_per_year=1.0)

    @pytest.mark.asyncio
    async def test_declaration_is_stored_on_the_baseline(self):
        record = _record(baseline=RecommendationBaseline(sample_count=3))
        declared = UserDeclaredSavings(cost_per_year=12.0, flags=["x"])
        stored = {}

        class Repo:
            async def update_recommendation_fields(self, rec_id, updates):
                stored.update(updates)
                return record.model_copy(update={"baseline": RecommendationBaseline(**updates["baseline"])})

        updated = await record_user_declared_savings(Repo(), record, declared)
        assert stored["baseline"]["sample_count"] == 3
        restored = user_declared_savings(updated)
        assert restored is not None and restored.cost_per_year == 12.0


class _Projector:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def sync(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ledger v2 unavailable")


@pytest.mark.asyncio
async def test_accrual_is_dual_written_to_the_ledger_v2():
    """C6: every run that reached the database is followed by a ledger v2 pass."""
    projector = _Projector()
    attributor = SavingsAttributor(FakeLedger(), "c", ledger=projector)
    result = await attributor.accrue([_record()], now=NOW)
    assert result.written > 0 and projector.calls == 1


@pytest.mark.asyncio
async def test_a_failed_ledger_v2_pass_never_fails_the_accrual():
    from greenkube.verify.ledger.projector import LEDGER_WRITE_ERRORS

    before = LEDGER_WRITE_ERRORS.labels(reason="dual_write_failed")._value.get()
    ledger = FakeLedger()
    attributor = SavingsAttributor(ledger, "c", ledger=_Projector(fail=True))
    result = await attributor.accrue([_record()], now=NOW)
    assert result.written > 0 and result.errors == 0 and ledger.rows
    assert LEDGER_WRITE_ERRORS.labels(reason="dual_write_failed")._value.get() == before + 1


@pytest.mark.asyncio
async def test_no_ledger_v2_pass_when_the_v1_write_failed():
    ledger = FakeLedger()
    ledger.fail_append = True
    projector = _Projector()
    await SavingsAttributor(ledger, "c", ledger=projector).accrue([_record()], now=NOW)
    assert projector.calls == 0


class _ClockedLedger(FakeLedger):
    """A ledger whose database clock is set by the test (LED-06)."""

    def __init__(self, database_time: Optional[datetime]) -> None:
        super().__init__()
        self.database_time = database_time

    async def get_database_time(self) -> Optional[datetime]:
        return self.database_time


class TestClockSkew:
    """LED-06: a wrong process clock never books future slots nor freezes accrual."""

    @pytest.mark.asyncio
    async def test_bad_clock_runs_write_nothing_and_count_clock_skew(self):
        ledger = _ClockedLedger(NOW)
        ledger.watermarks[("c", "recommendation_savings")] = NOW.replace(minute=0)
        bad = SavingsAttributor(ledger, "c", clock=lambda: NOW + timedelta(days=400))
        before = _errors("clock_skew")

        results = [await bad.accrue([_record()]) for _ in range(3)]

        assert all(result.written == 0 and result.clock_skew for result in results)
        assert _errors("clock_skew") == before + 3
        assert not ledger.rows
        assert ledger.watermarks[("c", "recommendation_savings")] == NOW.replace(minute=0)

    @pytest.mark.asyncio
    async def test_clock_within_tolerance_accrues(self):
        ledger = _ClockedLedger(NOW + timedelta(minutes=2))
        result = await SavingsAttributor(ledger, "c", clock=lambda: NOW).accrue([_record()])
        assert not result.clock_skew and result.written == 3

    @pytest.mark.asyncio
    async def test_change_applied_while_the_watermark_is_ahead_accrues_from_applied_at(self):
        """A watermark left in the future by an earlier bad clock never freezes accrual."""
        from greenkube.core.savings_attributor import ACCRUAL_WATERMARK_AHEAD

        ledger = FakeLedger()
        ahead = NOW.replace(minute=0) + timedelta(days=21)
        ledger.watermarks[("c", "recommendation_savings")] = ahead
        record = _record(applied_at=NOW + timedelta(hours=1))
        attributor = SavingsAttributor(ledger, "c")

        await attributor.accrue([record], now=NOW + timedelta(hours=4))
        assert ledger.method_sum("projected") == pytest.approx(53 / 60 + 2)
        await attributor.accrue([record], now=NOW + timedelta(hours=6))
        assert ledger.method_sum("projected") == pytest.approx(53 / 60 + 4)
        # No slot beyond the trusted now, and the watermark ahead is exposed.
        assert max(r.period_end for r in ledger.rows.values() if r.period_end) <= NOW + timedelta(hours=6)
        assert ACCRUAL_WATERMARK_AHEAD.labels(cluster_name="c")._value.get() > 0
        assert ledger.watermarks[("c", "recommendation_savings")] == ahead


class TestPerRecommendationCursor:
    """LED-12 (c): a record that fails for some runs keeps the slots of those runs."""

    @pytest.mark.asyncio
    async def test_record_invalid_for_three_runs_then_fixed_has_no_missing_slot(self):
        ledger = FakeLedger()
        attributor = SavingsAttributor(ledger, "c")
        good = _record(1, applied_at=NOW - timedelta(hours=1))
        await attributor.accrue([good], now=NOW)
        broken = _record(1, applied_at=NOW - timedelta(hours=1), co2e=float("nan"))
        for hour in (1, 2, 3):
            result = await attributor.accrue([broken], now=NOW + timedelta(hours=hour))
            assert result.errors == 1
        await attributor.accrue([good], now=NOW + timedelta(hours=4))

        slots = sorted(r.slot_start for r in ledger.rows.values() if r.slot_start is not None)
        expected = [NOW.replace(minute=0) + timedelta(hours=h) for h in range(-1, 4)]
        assert slots == expected
        assert ledger.method_sum("projected") == pytest.approx(53 / 60 + 4)
