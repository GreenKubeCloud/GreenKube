# src/greenkube/verify/drift.py
"""Drift monitor: realized savings that do not hold (roadmap C8).

A realized change keeps accruing its measured saving. The drift monitor
watches it after the measurement and acts when the saving is lost:

* **Drift.** For a request rightsizing the saving is the allocated cost freed
  per replica (requests x unit price), so the monitor compares the daily
  allocated cost *per replica* with the baseline snapshot (``B``) and with the
  measured post-window (``A``). The share of the saving lost on day ``d`` is
  ``(rate_d - A) / (B - A)``. Per-replica rates are not moved by load-driven
  replica counts; they move when requests creep back (a VPA in auto mode, an
  admission webhook, a LimitRange) or prices change. When the lost share is at
  least ``CHANGE_DRIFT_THRESHOLD`` on each of the last
  ``CHANGE_DRIFT_SUSTAINED_DAYS`` complete UTC days, the change becomes
  ``regressed`` (``realized → regressed``, event ``regressed`` with the daily
  evidence) and its verified recommendations ``regressed``, which stops their
  accrual.
* **Reversal.** The measured accruals written from the drift onset (the first
  day of the sustained run) until accrual stopped are reversed by one signed
  ``measured`` ledger row per recommendation covering ``[onset, last accrued
  slot)``. It is written once the accrual has settled (two hours after the
  regression), keyed by recommendation and period so it is written once, and
  recorded on the change as a ``savings_reversed`` event. The savings
  counters report it as reversed.
* **Spec drift.** A pod template that differs from the change's own
  (``spec_digest_after``) or a recreated workload, observed after the
  measurement without a newer recorded change, closes the change
  (``realized → closed``, event ``spec_drift`` or ``workload_recreated``):
  the measured saving no longer describes what runs. A newer recorded change
  closes it at record time (``superseded``).

Fail closed: a day with fewer than :data:`MIN_DAY_HOURS` hours of trusted cost
does not count either way, a change whose post-window shows no per-replica
saving is not assessed, and a failed read leaves the change as it is until the
next pass.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from greenkube.utils.time import ensure_utc
from greenkube.verify.model import (
    ChangeEvent,
    ChangeStatus,
    HourBucket,
    MeasurementState,
    TimeWindow,
)
from greenkube.verify.series import hourly_buckets

if TYPE_CHECKING:
    from greenkube.verify.service import ChangeMeasurementService

logger = logging.getLogger(__name__)

#: Days after the post-window read on each pass (the longest drift run whose onset is found).
DRIFT_LOOKBACK_DAYS = 7
#: Hours with trusted cost for a day to count.
MIN_DAY_HOURS = 18
#: The reversal waits this long after the regression, so an accrual run in flight has finished.
REVERSAL_SETTLE = timedelta(hours=2)
#: Interval of the drift monitor loop.
DRIFT_MONITOR_INTERVAL_SECONDS = 3600.0
_SECONDS_PER_YEAR = 365.25 * 24 * 3600
#: The day each (change, measurement) was last assessed: drift is decided on complete days, so a
#: realized change is read once per day (per process), not on every pass.
_ASSESSED: Dict[tuple, datetime] = {}
_SLOT = timedelta(hours=1)
_MIN_COVERAGE = 0.5


@dataclass(frozen=True)
class DriftSettings:
    """``threshold``: share of the realized saving lost; ``sustained_days``: consecutive days beyond it."""

    threshold: float = 0.5
    sustained_days: int = 3

    @classmethod
    def from_config(cls, config: Any) -> "DriftSettings":
        return cls(
            threshold=float(getattr(config, "CHANGE_DRIFT_THRESHOLD", 0.5)),
            sustained_days=int(getattr(config, "CHANGE_DRIFT_SUSTAINED_DAYS", 3)),
        )


@dataclass(frozen=True)
class DriftDay:
    start: datetime
    hours: int
    rate_per_replica: float
    lost_share: float

    def as_dict(self) -> dict:
        return {
            "day": self.start.date().isoformat(),
            "hours": self.hours,
            "cost_per_replica_hour": round(self.rate_per_replica, 9),
            "lost_share": round(self.lost_share, 4),
        }


@dataclass(frozen=True)
class DriftAssessment:
    """Daily lost shares of a realized saving and the verdict."""

    baseline_per_replica: Optional[float]
    realized_per_replica: Optional[float]
    days: List[DriftDay] = field(default_factory=list)
    regressed: bool = False
    onset: Optional[datetime] = None
    reason: str = ""

    def payload(self, settings: DriftSettings) -> dict:
        return {
            "quantity": "allocated_cost_per_replica",
            "baseline_per_replica_hour": _round(self.baseline_per_replica),
            "realized_per_replica_hour": _round(self.realized_per_replica),
            "threshold": settings.threshold,
            "sustained_days": settings.sustained_days,
            "onset": self.onset.isoformat() if self.onset is not None else None,
            "days": [day.as_dict() for day in self.days],
        }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 9)


def per_replica_cost(buckets: Sequence[HourBucket]) -> Dict[datetime, float]:
    """Allocated cost per replica-hour of every hour with trusted cost and running pods."""
    series: Dict[datetime, float] = {}
    for bucket in buckets:
        if (
            bucket.cost is None
            or bucket.cost_coverage < _MIN_COVERAGE
            or not bucket.replicas
            or bucket.replicas <= 0
            or not math.isfinite(bucket.cost)
        ):
            continue
        series[ensure_utc(bucket.hour)] = float(bucket.cost) / float(bucket.replicas)
    return series


def _mean(values: Sequence[float]) -> Optional[float]:
    return math.fsum(values) / len(values) if values else None


def assess_drift(
    baseline: Sequence[HourBucket],
    post: Sequence[HourBucket],
    monitored: Sequence[HourBucket],
    settings: DriftSettings,
) -> DriftAssessment:
    """Lost share of the per-replica saving on each complete day of ``monitored``."""
    before = _mean(list(per_replica_cost(baseline).values()))
    after = _mean(list(per_replica_cost(post).values()))
    if before is None or after is None:
        return DriftAssessment(before, after, reason="no trusted per-replica cost in the baseline or post-window")
    saving = before - after
    if saving <= 0 or saving <= 1e-9 * abs(before):
        return DriftAssessment(before, after, reason="the post-window shows no per-replica saving to monitor")
    by_day: Dict[datetime, List[float]] = {}
    for hour, value in per_replica_cost(monitored).items():
        day = hour.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day.setdefault(day, []).append(value)
    days: List[DriftDay] = []
    for day in sorted(by_day):
        values = by_day[day]
        if len(values) < MIN_DAY_HOURS:
            continue
        rate = math.fsum(values) / len(values)
        days.append(DriftDay(start=day, hours=len(values), rate_per_replica=rate, lost_share=(rate - after) / saving))
    run: List[DriftDay] = []
    for day in reversed(days):
        if day.lost_share < settings.threshold or (run and run[-1].start - day.start != timedelta(days=1)):
            break
        run.append(day)
    if len(run) < settings.sustained_days:
        return DriftAssessment(before, after, days=days, reason="the saving holds")
    onset = run[-1].start
    lost = _mean([day.lost_share for day in run]) or 0.0
    return DriftAssessment(
        before,
        after,
        days=days,
        regressed=True,
        onset=onset,
        reason=(
            f"{lost:.0%} of the realized per-replica saving was lost on each of the last {len(run)} days "
            f"(since {onset.date().isoformat()}; threshold {settings.threshold:.0%} for "
            f"{settings.sustained_days} days)"
        ),
    )


class DriftMonitor:
    """Watches realized changes and regresses or closes those whose saving no longer holds (C8)."""

    def __init__(
        self,
        service: "ChangeMeasurementService",
        *,
        savings_repo: Any = None,
        settings: Optional[DriftSettings] = None,
        cluster_name: str = "default",
        assessed: Optional[Dict[tuple, datetime]] = None,
    ):
        self.service = service
        self.changes = service.changes
        self.lifecycle = service.lifecycle
        self.savings_repo = savings_repo
        self.settings = settings or DriftSettings.from_config(service.config)
        self.cluster_name = cluster_name
        self._assessed = _ASSESSED if assessed is None else assessed

    async def run(self, now: Optional[datetime] = None) -> Dict[str, List[str]]:
        """One pass: returns the ids of the changes regressed, closed and reversed."""
        moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        result: Dict[str, List[str]] = {"regressed": [], "closed": [], "reversed": []}
        if not self.service.enabled:
            return result
        for status in await self.changes.list_statuses([MeasurementState.REALIZED.value]):
            try:
                outcome = await self._watch(status, moment)
            except Exception as exc:
                logger.warning("Drift check of change %s failed: %s", status.change_id, exc)
                continue
            if outcome is not None:
                result[outcome].append(status.change_id)
        for status in await self.changes.list_statuses([MeasurementState.REGRESSED.value]):
            try:
                if await self._reverse(status, moment):
                    result["reversed"].append(status.change_id)
            except Exception as exc:
                logger.warning("Savings reversal of change %s failed: %s", status.change_id, exc)
        return result

    # ------------------------------------------------------------------
    # Realized changes
    # ------------------------------------------------------------------

    async def _watch(self, status: ChangeStatus, now: datetime) -> Optional[str]:
        change = await self.changes.get_change(status.change_id)
        if change is None or status.post_window_end is None:
            return None
        if await self._close_on_spec_drift(change, status, now):
            return "closed"
        post_end = ensure_utc(status.post_window_end)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = max(post_end, today - timedelta(days=DRIFT_LOOKBACK_DAYS))
        if today - start < timedelta(days=self.settings.sustained_days):
            return None
        memo = (status.change_id, status.latest_measurement_id)
        if self._assessed.get(memo) == today:
            return None
        snapshot = await self.changes.get_snapshot_for_change(change.change_id)
        if snapshot is None:
            return None
        step = self.service._step()
        post_window = TimeWindow(
            start=ensure_utc(status.post_window_start or change.rollout_completed_at), end=post_end
        )
        post = hourly_buckets(await self.service._read_workload(change.target, post_window), post_window, step)
        window = TimeWindow(start=start, end=today)
        monitored = hourly_buckets(await self.service._read_workload(change.target, window), window, step)
        assessment = assess_drift(snapshot.hours, post, monitored, self.settings)
        self._assessed[memo] = today
        if not assessment.regressed:
            return None
        await self.changes.transition(
            status,
            MeasurementState.REGRESSED,
            event_type="regressed",
            actor="drift-monitor",
            reason=assessment.reason,
            payload=assessment.payload(self.settings),
            now=now,
        )
        await self._regress_members(change, assessment.reason)
        logger.info("Change %s regressed: %s", change.change_id, assessment.reason)
        return "regressed"

    async def _close_on_spec_drift(self, change: ChangeEvent, status: ChangeStatus, now: datetime) -> bool:
        observations = await self.changes.spec_observations(
            *change.target.key, since=ensure_utc(change.rollout_completed_at)
        )
        later = [
            item for item in observations if ensure_utc(item.observed_at) > ensure_utc(change.rollout_completed_at)
        ]
        if not later:
            return False
        latest = max(later, key=lambda item: ensure_utc(item.observed_at))
        if change.workload_uid and latest.workload_uid and latest.workload_uid != change.workload_uid:
            event, reason = "workload_recreated", "the workload was deleted and recreated after the measurement"
        elif change.spec_digest_after and latest.spec_digest and latest.spec_digest != change.spec_digest_after:
            event, reason = "spec_drift", "the pod template no longer matches the measured change"
        else:
            return False
        await self.changes.transition(
            status,
            MeasurementState.CLOSED,
            event_type=event,
            actor="drift-monitor",
            reason=reason,
            payload={
                "observed_at": ensure_utc(latest.observed_at).isoformat(),
                "spec_digest": latest.spec_digest,
                "workload_uid": latest.workload_uid,
            },
            now=now,
        )
        return True

    async def _regress_members(self, change: ChangeEvent, reason: str) -> None:
        for record in await self.service._records(change.recommendation_ids):
            if record.id is None or str(getattr(record.status, "value", record.status)) != "verified":
                continue
            try:
                await self.lifecycle.mark_regressed(record.id, reasons=[reason], actor="drift-monitor")
            except Exception as exc:
                logger.warning("Could not mark recommendation %s regressed: %s", record.id, exc)

    # ------------------------------------------------------------------
    # Regressed changes: reversal entries
    # ------------------------------------------------------------------

    async def _reverse(self, status: ChangeStatus, now: datetime) -> bool:
        """Reverse the measured accruals written since the drift onset (once accrual has settled)."""
        if self.savings_repo is None:
            return False
        events = await self.changes.list_events(status.change_id)
        regressed = next((event for event in reversed(events) if event.event_type == "regressed"), None)
        if regressed is None or not regressed.payload.get("onset"):
            return False
        if now - ensure_utc(regressed.created_at) < REVERSAL_SETTLE:
            return False
        change = await self.changes.get_change(status.change_id)
        if change is None:
            return False
        # A crash between the transition and the members' regression is repaired here.
        await self._regress_members(change, regressed.reason)
        onset = ensure_utc(regressed.payload["onset"])
        records = await self.service._records(change.recommendation_ids)
        ids = [record.id for record in records if record.id is not None]
        floors = await self.savings_repo.get_accrual_floors(ids) if ids else {}
        rows = []
        entries = []
        for record in records:
            if record.id is None or str(getattr(record.status, "value", record.status)) != "regressed":
                continue
            row = self._reversal(record, onset, floors.get(record.id))
            if row is not None:
                rows.append(row)
                entries.append(
                    {
                        "recommendation_id": record.id,
                        "period_start": row.period_start.isoformat() if row.period_start else None,
                        "period_end": row.period_end.isoformat() if row.period_end else None,
                        "cost": row.cost_saved_dollars,
                        "co2e_g": row.co2e_saved_grams,
                    }
                )
        if not rows:
            return False
        inserted = await self.savings_repo.save_records(rows)
        if inserted <= 0:
            return False
        await self.changes.record_event(
            status.change_id,
            "savings_reversed",
            actor="drift-monitor",
            reason=f"measured accruals since {onset.isoformat()} reversed",
            payload={"entries": entries},
            now=now,
        )
        return True

    def _reversal(self, record: Any, onset: datetime, floor: Optional[datetime]):
        from greenkube.models.savings import METHOD_MEASURED, SavingsLedgerRecord

        if floor is None:
            return None
        start = onset
        if record.verified_at is not None:
            # Measured accrual starts at verification; earlier slots accrued the projection.
            start = max(start, ensure_utc(record.verified_at))
        end = ensure_utc(floor)
        # A one-slot period would take the slot's idempotency key, which its accrual holds.
        if end - start <= _SLOT:
            return None
        seconds = (end - start).total_seconds()
        factor = seconds / _SECONDS_PER_YEAR
        cost = float(record.measured_cost_saved or 0.0)
        co2e = float(record.measured_co2e_saved_grams or 0.0)
        if cost == 0.0 and co2e == 0.0:
            return None
        return SavingsLedgerRecord(
            recommendation_id=record.id,
            cluster_name=self.cluster_name,
            namespace=record.namespace or "",
            recommendation_type=str(getattr(record.type, "value", record.type)),
            co2e_saved_grams=-co2e * factor,
            cost_saved_dollars=-cost * factor,
            period_seconds=max(1, math.ceil(seconds)),
            timestamp=end,
            period_start=start,
            period_end=end,
            slot_start=None,
            measurement_method=METHOD_MEASURED,
            baseline_value=record.potential_savings_co2e_grams,
            actual_value=-co2e,
        )


async def run_drift_monitor() -> None:
    """Controller loop job: one drift-monitor pass over realized and regressed changes (C8)."""
    from greenkube.core.config import get_config
    from greenkube.core.factory import (
        get_combined_metrics_repository,
        get_recommendation_repository,
        get_savings_ledger_repository,
    )
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle
    from greenkube.core.savings_attributor import savings_cluster_name
    from greenkube.storage.change_repository import get_change_repository
    from greenkube.verify.service import ChangeMeasurementService

    config = get_config()
    lifecycle = RecommendationLifecycle(get_recommendation_repository())
    service = ChangeMeasurementService(
        lifecycle, get_combined_metrics_repository(), get_change_repository(), config=config
    )
    monitor = DriftMonitor(
        service,
        savings_repo=get_savings_ledger_repository(),
        settings=DriftSettings.from_config(config),
        cluster_name=savings_cluster_name(),
    )
    result = await monitor.run()
    if any(result.values()):
        logger.info(
            "Drift monitor: %d regressed, %d closed, %d reversed.",
            len(result["regressed"]),
            len(result["closed"]),
            len(result["reversed"]),
        )


__all__ = ["DriftAssessment", "DriftMonitor", "DriftSettings", "assess_drift", "run_drift_monitor"]
