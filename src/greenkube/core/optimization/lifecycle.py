# src/greenkube/core/optimization/lifecycle.py
"""Recommendation lifecycle state machine and audit-trail emission.

Every transition goes through :class:`RecommendationLifecycle` so that the
``recommendation_events`` audit trail is always written alongside the status
change. Apply detection and verification jobs use the same service.

Transitions are compare-and-swap: the status read before validating a
transition must still be the stored status when the update and its event are
written (atomically, in one transaction, when the repository supports it).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, List, Optional

from greenkube.models.metrics import (
    ApplicationMethod,
    ApplyRecommendationRequest,
    RecommendationEvent,
    RecommendationEventType,
    RecommendationRecord,
    RecommendationStatus,
    VerificationStatus,
)
from greenkube.utils.time import ensure_utc

if TYPE_CHECKING:
    from greenkube.storage.base_repository import RecommendationRepository

logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS = {
    RecommendationStatus.ACTIVE.value: {
        RecommendationStatus.APPLIED.value,
        RecommendationStatus.IGNORED.value,
        RecommendationStatus.STALE.value,
        RecommendationStatus.EXPIRED.value,
        RecommendationStatus.PR_OPEN.value,
    },
    RecommendationStatus.PR_OPEN.value: {
        RecommendationStatus.APPLIED.value,
        RecommendationStatus.FAILED.value,
        # The pull request was closed unmerged or its action failed: offer it again.
        RecommendationStatus.ACTIVE.value,
    },
    RecommendationStatus.APPLIED.value: {
        RecommendationStatus.VERIFYING.value,
        RecommendationStatus.VERIFIED.value,
        RecommendationStatus.ROLLBACK_REVIEW.value,
        RecommendationStatus.REVERTED.value,
        RecommendationStatus.FAILED.value,
    },
    RecommendationStatus.VERIFYING.value: {
        RecommendationStatus.APPLIED.value,
        RecommendationStatus.VERIFIED.value,
        RecommendationStatus.ROLLBACK_REVIEW.value,
        RecommendationStatus.FAILED.value,
    },
    # C8 (DriftMonitor) will drive this transition; the state exists so a
    # realized saving is never a dead end.
    RecommendationStatus.VERIFIED.value: {RecommendationStatus.REGRESSED.value},
    RecommendationStatus.ROLLBACK_REVIEW.value: {
        RecommendationStatus.REVERTED.value,
        # An operator dismisses a false-positive health finding (with a reason).
        RecommendationStatus.APPLIED.value,
    },
    RecommendationStatus.IGNORED.value: {RecommendationStatus.ACTIVE.value},
    # Nothing of a stale or expired recommendation is in the cluster or in flight.
    RecommendationStatus.STALE.value: {RecommendationStatus.IGNORED.value},
    RecommendationStatus.EXPIRED.value: {RecommendationStatus.IGNORED.value},
}

#: Statuses an operator may ignore (C0.11). An open PR or an applied change is
#: refused: ignoring it would orphan the change and its measurement. Ignoring
#: an ignored recommendation again only updates its reason.
IGNORABLE_STATUSES = frozenset(
    {
        RecommendationStatus.ACTIVE.value,
        RecommendationStatus.STALE.value,
        RecommendationStatus.EXPIRED.value,
        RecommendationStatus.IGNORED.value,
    }
)

#: Statuses PATCH /apply accepts (C0.11): re-applying any other status would
#: overwrite the verification baseline of a change that already landed.
APPLICABLE_STATUSES = frozenset({RecommendationStatus.ACTIVE.value, RecommendationStatus.PR_OPEN.value})

#: Verification statuses from which an explicit re-measurement is allowed (C0.5).
REMEASURABLE_VERIFICATION_STATUSES = frozenset({VerificationStatus.FAILED.value, VerificationStatus.INCONCLUSIVE.value})


class RecommendationNotFoundError(ValueError):
    """Raised when the recommendation does not exist."""


class RecommendationTransitionError(ValueError):
    """Raised when a lifecycle transition is not allowed from the current state."""


class RecommendationConflictError(RecommendationTransitionError):
    """Raised when the record changed between validation and write (compare-and-swap miss)."""


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _status_value(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _baseline_dict(record: RecommendationRecord) -> dict:
    baseline = record.baseline
    if baseline is None:
        return {}
    if hasattr(baseline, "model_dump"):
        return baseline.model_dump(mode="json")
    return dict(baseline)


def _group_ids(record: RecommendationRecord, siblings: Iterable[RecommendationRecord]) -> List[int]:
    """Sorted IDs of a record and the other members of its attribution group."""
    return sorted({member.id for member in (record, *siblings) if member.id is not None})


def freeze_baseline(
    record: RecommendationRecord,
    *,
    baseline_window_end: Optional[datetime] = None,
    frozen_at: Optional[datetime] = None,
) -> dict:
    """Captures the pre-apply evidence snapshot used as the verification baseline.

    ``captured_at`` is the end of the evidence window the snapshot copies (or
    the recommendation's creation time without evidence, and the freeze time
    only when neither is known), not the time the baseline was frozen;
    ``frozen_at`` records the latter. ``baseline_window_end``
    is the end of the measured baseline window (PR merge or plan approval), so
    the verifier never lets the baseline overlap the after-window (C0.12).
    """
    evidence = record.evidence
    frozen = frozen_at or datetime.now(timezone.utc)
    evidence_end = evidence.observation_window_end if evidence is not None else None
    baseline: dict = {
        "captured_at": _iso(evidence_end or record.created_at or frozen),
        "frozen_at": frozen.isoformat(),
        "current_cpu_request_millicores": record.current_cpu_request_millicores,
        "current_memory_request_bytes": record.current_memory_request_bytes,
        "recommended_cpu_request_millicores": record.recommended_cpu_request_millicores,
        "recommended_memory_request_bytes": record.recommended_memory_request_bytes,
        "potential_savings_cost": record.potential_savings_cost,
        "potential_savings_co2e_grams": record.potential_savings_co2e_grams,
        "measurement_sequence": 1,
        "measurement_ids": [],
    }
    if baseline_window_end is not None:
        baseline["baseline_window_end"] = ensure_utc(baseline_window_end).isoformat()
    if evidence is not None:
        baseline.update(
            {
                "evidence_window": {
                    "start": _iso(evidence.observation_window_start),
                    "end": _iso(evidence.observation_window_end),
                },
                "observation_window_start": _iso(evidence.observation_window_start),
                "observation_window_end": _iso(evidence.observation_window_end),
                "observation_window_seconds": evidence.observation_window_seconds,
                "cost_per_hour_before": evidence.cost_per_hour_before,
                "co2e_grams_per_hour_before": evidence.co2e_grams_per_hour_before,
                "restart_count": evidence.restart_count,
                "cpu_usage": evidence.cpu_usage.model_dump() if evidence.cpu_usage else None,
                "memory_usage": evidence.memory_usage.model_dump() if evidence.memory_usage else None,
                "current": evidence.current.model_dump(),
                "proposed": evidence.proposed.model_dump(),
                "savings_method": evidence.savings_method,
            }
        )
    return baseline


def _event_time(event: RecommendationEvent) -> datetime:
    return ensure_utc(event.created_at)


#: Plan statuses that end a plan without delivering it: its approval decided nothing.
_ABANDONED_PLAN_STATUSES = frozenset({"closed", "stale", "failed", "rejected", "superseded", "cancelled"})


def _abandoned_plan_ids(events: Iterable[RecommendationEvent]) -> set[str]:
    """Plans whose latest recorded status ends them without delivery (closed, stale, failed...)."""
    latest: dict[str, tuple[datetime, Optional[str]]] = {}
    for event in events:
        if event.event_type != "plan_status_changed":
            continue
        details = getattr(event.payload, "details", None) or {}
        linked = details.get("plan_id")
        if not linked:
            continue
        moment = _event_time(event)
        if linked not in latest or moment >= latest[linked][0]:
            latest[linked] = (moment, details.get("new_status"))
    return {linked for linked, (_, status) in latest.items() if status in _ABANDONED_PLAN_STATUSES}


def _plan_status_event_time(
    events: Iterable[RecommendationEvent],
    new_status: str,
    plan_id: Optional[str],
    *,
    skip_abandoned: bool = False,
) -> Optional[datetime]:
    """Return the latest time a linked plan entered ``new_status``.

    With ``skip_abandoned``, plans that ended without delivery (closed, stale,
    failed, rejected, superseded) are left out.
    """
    events = list(events)
    abandoned: set[str] = _abandoned_plan_ids(events) if skip_abandoned else set()
    latest: Optional[datetime] = None
    for event in events:
        if event.event_type != "plan_status_changed":
            continue
        details = getattr(event.payload, "details", None) or {}
        if details.get("new_status") != new_status:
            continue
        if plan_id is not None and details.get("plan_id") not in (None, plan_id):
            continue
        if details.get("plan_id") in abandoned:
            continue
        moment = _event_time(event)
        if latest is None or moment > latest:
            latest = moment
    return latest


def _mark_accrual_resumption(current: RecommendationRecord, changes: dict) -> None:
    """Stamp ``baseline.accruing_since`` when an applied record that had stopped accruing accrues again (LED-12).

    The attributor accrues each record from its own cursor; without this mark
    a record dismissed from a rollback review, or re-measured after a refuted
    measurement, would backfill the hours it did not accrue. A first apply is
    not marked, so a change detected late keeps its history from applied_at.
    """
    from ..savings_attributor import ACCRUING_SINCE_KEY, accrues_in

    if current.applied_at is None:
        return
    before = accrues_in(current.status, current.verification_status)
    after = accrues_in(
        changes.get("status", current.status), changes.get("verification_status", current.verification_status)
    )
    if before or not after:
        return
    baseline = changes.get("baseline")
    if baseline is None:
        baseline = current.baseline
    if baseline is None:
        payload: dict = {}
    elif hasattr(baseline, "model_dump"):
        payload = baseline.model_dump(mode="json")
    else:
        payload = dict(baseline)
    moment = changes.get("updated_at") or datetime.now(timezone.utc)
    payload[ACCRUING_SINCE_KEY] = ensure_utc(moment).isoformat()
    changes["baseline"] = payload


class RecommendationLifecycle:
    """Applies lifecycle transitions and writes the matching audit events."""

    def __init__(self, repo: "RecommendationRepository"):
        self.repo = repo

    async def record_event(
        self,
        rec_id: int,
        event_type: RecommendationEventType | str,
        *,
        actor: str = "system",
        payload: Optional[dict] = None,
    ) -> RecommendationEvent:
        """Persists one audit event."""
        value = _status_value(event_type)
        event = RecommendationEvent(
            recommendation_id=rec_id,
            event_type=value,
            actor=actor,
            payload=payload or {},
        )
        try:
            return await self.repo.record_event(event)
        except Exception:
            logger.exception("Could not record recommendation event %s for %s.", value, rec_id)
            raise

    async def transition(
        self,
        rec_id: int,
        status: RecommendationStatus | str,
        *,
        event_type: RecommendationEventType | str,
        updates: Optional[dict] = None,
        actor: str = "system",
        payload: Optional[dict] = None,
        emit_event: bool = True,
        expected_statuses: Optional[Iterable[RecommendationStatus | str]] = None,
    ) -> RecommendationRecord:
        """Updates a record's status/fields and emits its audit event.

        The update is a compare-and-swap on the status read here: a concurrent
        transition raises :class:`RecommendationConflictError` instead of being
        overwritten. ``expected_statuses`` further restricts the source states.
        """
        status_value = _status_value(status)
        changes = dict(updates or {})
        changes["status"] = status_value
        changes.setdefault("updated_at", datetime.now(timezone.utc))
        current = await self.repo.get_recommendation_by_id(rec_id)
        if current is None:
            raise RecommendationNotFoundError(f"Recommendation {rec_id} not found.")
        current_value = _status_value(current.status)
        if expected_statuses is not None and current_value not in {_status_value(item) for item in expected_statuses}:
            raise RecommendationTransitionError(
                f"Recommendation {rec_id} is {current_value}; expected one of "
                f"{sorted(_status_value(item) for item in expected_statuses)}."
            )
        if status_value != current_value and status_value not in _ALLOWED_TRANSITIONS.get(current_value, set()):
            raise RecommendationTransitionError(
                f"Invalid recommendation transition: {current_value} -> {status_value}."
            )
        _mark_accrual_resumption(current, changes)
        if emit_event and hasattr(self.repo, "transition_and_record_event"):
            record = await self.repo.transition_and_record_event(
                rec_id,
                changes,
                _status_value(event_type),
                actor,
                payload or {},
                expected_statuses=[current_value],
            )
            if record is None:
                raise RecommendationConflictError(
                    f"Recommendation {rec_id} changed concurrently; {current_value} -> {status_value} was not applied."
                )
        else:
            record = await self.repo.update_recommendation_fields(rec_id, changes)
            if emit_event:
                await self.record_event(rec_id, event_type, actor=actor, payload=payload)
        return record

    async def apply(
        self,
        rec_id: int,
        *,
        actual_cpu: Optional[int] = None,
        actual_memory: Optional[int] = None,
        carbon_saved_co2e_grams: Optional[float] = None,
        cost_saved: Optional[float] = None,
        application_method: str = ApplicationMethod.MANUAL.value,
        actor: str = "user",
        applied_at: Optional[datetime] = None,
        baseline_window_end: Optional[datetime] = None,
        plan_id: Optional[str] = None,
        rollout: Optional[dict] = None,
        change_id: Optional[str] = None,
    ) -> RecommendationRecord:
        """Marks an ``active``/``pr_open`` recommendation applied and freezes its baseline.

        ``applied_at`` is the rollout completion time (C0.12); it defaults to
        now. The baseline window ends at ``baseline_window_end`` when given,
        otherwise at the latest PR merge (or plan approval) recorded for the
        recommendation, otherwise at ``applied_at``. ``change_id`` links the
        record to the change recorded by apply detection (C1); such records are
        measured by the change-measurement pipeline.
        """
        from greenkube.core.recommendation_realization import estimate_realized_savings

        record = await self.repo.get_recommendation_by_id(rec_id)
        if record is None:
            raise RecommendationNotFoundError(f"Recommendation {rec_id} not found.")
        current_status = _status_value(record.status)
        if current_status not in APPLICABLE_STATUSES:
            raise RecommendationTransitionError(
                f"Recommendation {rec_id} is {current_status}; only active or pr_open recommendations can be applied."
            )

        now = datetime.now(timezone.utc)
        applied = ensure_utc(applied_at) if applied_at is not None else now
        if applied > now:
            applied = now
        window_end = baseline_window_end
        if window_end is None:
            window_end = await self._decision_time(rec_id, plan_id)
        window_end = min(ensure_utc(window_end), applied) if window_end is not None else applied

        baseline = freeze_baseline(record, baseline_window_end=window_end, frozen_at=now)
        if plan_id is not None:
            baseline["plan_id"] = plan_id
        if rollout:
            baseline["rollout"] = rollout
        if change_id is not None:
            baseline["change_id"] = change_id
        request = ApplyRecommendationRequest(
            actual_cpu_request_millicores=actual_cpu,
            actual_memory_request_bytes=actual_memory,
            carbon_saved_co2e_grams=carbon_saved_co2e_grams,
            cost_saved=cost_saved,
            application_method=application_method,
        )
        carbon_saved, cost_value = estimate_realized_savings(record, request)
        return await self.transition(
            rec_id,
            RecommendationStatus.APPLIED,
            event_type=RecommendationEventType.APPLIED,
            updates={
                "applied_at": applied,
                "actual_cpu_request_millicores": actual_cpu,
                "actual_memory_request_bytes": actual_memory,
                "carbon_saved_co2e_grams": carbon_saved,
                "cost_saved": cost_value,
                "application_method": application_method,
                "baseline": baseline,
                "verification_status": VerificationStatus.PENDING.value,
                "verification_window_start": None,
                "verification_window_end": None,
                "verified_at": None,
                "measured_co2e_saved_grams": None,
                "measured_cost_saved": None,
                "savings_realized": None,
                "updated_at": now,
            },
            actor=actor,
            payload={
                "application_method": application_method,
                "applied_at": applied.isoformat(),
                "baseline_window_end": window_end.isoformat(),
                "plan_id": plan_id,
                **({"change_id": change_id} if change_id is not None else {}),
            },
            expected_statuses=APPLICABLE_STATUSES,
        )

    async def _decision_time(self, rec_id: int, plan_id: Optional[str]) -> Optional[datetime]:
        """Latest PR merge (or, without one, plan approval) recorded for the recommendation."""
        get_events = getattr(self.repo, "get_events", None)
        if get_events is None:
            return None
        try:
            events = await get_events(rec_id)
        except Exception as exc:
            logger.warning("Could not read events of recommendation %s for its baseline window: %s", rec_id, exc)
            return None
        merged = _plan_status_event_time(events, "merged", plan_id)
        if merged is not None:
            return merged
        # Without a known plan (manual or detected apply), the approval of a plan that
        # was then closed, went stale or failed did not decide this change.
        return _plan_status_event_time(events, "approved", plan_id, skip_abandoned=plan_id is None)

    async def expire(self, now: Optional[datetime] = None) -> List[RecommendationRecord]:
        """Marks TTL-elapsed active recommendations as expired and records events.

        Each expiry is a compare-and-swap from ``active``, written with its
        event in one transaction (C0.11): a recommendation applied, opened as a
        PR or ignored meanwhile is left alone.
        """
        if not hasattr(self.repo, "transition_and_record_event"):
            expired = await self.repo.expire_recommendations(now=now)
            for record in expired:
                if record.id is None:
                    continue
                await self.record_event(
                    record.id,
                    RecommendationEventType.EXPIRED,
                    payload={"expires_at": _iso(record.expires_at)},
                )
            return expired

        reference = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        active = await self.repo.get_recommendations_by_statuses([RecommendationStatus.ACTIVE.value])
        expired = []
        for record in active or []:
            if record.id is None or record.expires_at is None or ensure_utc(record.expires_at) >= reference:
                continue
            try:
                expired.append(
                    await self.transition(
                        record.id,
                        RecommendationStatus.EXPIRED,
                        event_type=RecommendationEventType.EXPIRED,
                        updates={"updated_at": reference},
                        payload={"expires_at": _iso(record.expires_at)},
                        expected_statuses=(RecommendationStatus.ACTIVE,),
                    )
                )
            except RecommendationTransitionError as exc:
                logger.info("Recommendation %s changed before it could expire: %s", record.id, exc)
        return expired

    async def mark_no_effective_change(
        self,
        rec_id: int,
        *,
        payload: Optional[dict] = None,
        actor: str = "detector",
    ) -> RecommendationRecord:
        """Retire an active recommendation whose proposal is already live (``no_effective_change``).

        The live request already equals the proposal, but not because this
        recommendation was applied: the change is already recorded on another
        recommendation of the same target, or it landed before this one was
        generated (a scan built from the lagging usage rollup). Applying it
        again would open a no-op pull request, and recording it would count
        the same change twice, so it is marked ``stale`` (compare-and-swap from
        ``active``) with a ``stale`` audit event carrying the reason.
        """
        return await self.transition(
            rec_id,
            RecommendationStatus.STALE,
            event_type=RecommendationEventType.STALE,
            actor=actor,
            payload={"reason": "no_effective_change", **(payload or {})},
            expected_statuses=(RecommendationStatus.ACTIVE,),
        )

    async def ignore(
        self,
        rec_id: int,
        *,
        reason: Optional[str] = None,
        actor: str = "user",
    ) -> RecommendationRecord:
        """Permanently ignore a recommendation (compare-and-swap with its event, C0.11).

        Only recommendations with nothing in flight or in the cluster
        (:data:`IGNORABLE_STATUSES`) can be ignored.
        """
        now = datetime.now(timezone.utc)
        return await self.transition(
            rec_id,
            RecommendationStatus.IGNORED,
            event_type=RecommendationEventType.IGNORED,
            updates={"ignored_at": now, "ignored_reason": reason, "updated_at": now},
            actor=actor,
            payload={"reason": reason},
            expected_statuses=IGNORABLE_STATUSES,
        )

    async def unignore(self, rec_id: int, *, actor: str = "user") -> RecommendationRecord:
        """Restore an ignored recommendation to ``active`` (only from ``ignored``, C0.11)."""
        return await self.transition(
            rec_id,
            RecommendationStatus.ACTIVE,
            event_type=RecommendationEventType.UNIGNORED,
            updates={"ignored_at": None, "ignored_reason": None},
            actor=actor,
            expected_statuses=(RecommendationStatus.IGNORED,),
        )

    async def mark_verification_started(self, rec_id: int) -> RecommendationRecord:
        return await self.transition(
            rec_id,
            RecommendationStatus.VERIFYING,
            event_type=RecommendationEventType.VERIFICATION_STARTED,
            updates={"verification_status": VerificationStatus.IN_PROGRESS.value},
        )

    async def mark_verified(
        self,
        rec_id: int,
        *,
        measured_co2e_saved_grams: float,
        measured_cost_saved: float,
        verification_window_start: datetime,
        verification_window_end: datetime,
        baseline: Optional[dict] = None,
        details: Optional[dict] = None,
    ) -> RecommendationRecord:
        updates: dict[str, Any] = {
            "verified_at": datetime.now(timezone.utc),
            "verification_status": VerificationStatus.PASSED.value,
            "verification_window_start": verification_window_start,
            "verification_window_end": verification_window_end,
            "measured_co2e_saved_grams": measured_co2e_saved_grams,
            "measured_cost_saved": measured_cost_saved,
            "savings_realized": True,
        }
        if baseline is not None:
            updates["baseline"] = baseline
        return await self.transition(
            rec_id,
            RecommendationStatus.VERIFIED,
            event_type=RecommendationEventType.VERIFIED,
            updates=updates,
            payload={
                "measured_co2e_saved_grams": measured_co2e_saved_grams,
                "measured_cost_saved": measured_cost_saved,
                **(details or {}),
            },
            expected_statuses=(RecommendationStatus.APPLIED, RecommendationStatus.VERIFYING),
        )

    async def mark_rollback_review(
        self,
        rec_id: int,
        *,
        reasons: List[str],
        verification_window_start: Optional[datetime] = None,
        verification_window_end: Optional[datetime] = None,
        measured_co2e_saved_grams: Optional[float] = None,
        measured_cost_saved: Optional[float] = None,
        baseline: Optional[dict] = None,
        details: Optional[dict] = None,
    ) -> RecommendationRecord:
        updates: dict[str, Any] = {
            "verification_status": VerificationStatus.FAILED.value,
            "verification_window_start": verification_window_start,
            "verification_window_end": verification_window_end,
            "measured_co2e_saved_grams": measured_co2e_saved_grams,
            "measured_cost_saved": measured_cost_saved,
            "savings_realized": False,
        }
        if baseline is not None:
            updates["baseline"] = baseline
        return await self.transition(
            rec_id,
            RecommendationStatus.ROLLBACK_REVIEW,
            event_type=RecommendationEventType.ROLLBACK_REVIEW,
            updates=updates,
            payload={"reasons": reasons, **(details or {})},
        )

    async def dismiss_rollback_review(
        self,
        rec_id: int,
        *,
        reason: str,
        actor: str = "user",
        window_hours: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> RecommendationRecord:
        """Return a false-positive rollback review to ``applied`` with a fresh measurement window.

        The review was raised for the whole change, so every member of the
        record's attribution group (the CPU and memory records of one plan,
        C0.10) still in ``rollback_review`` is dismissed with it and measured
        again over the same window.
        """
        if not reason or not reason.strip():
            raise ValueError("A reason is required to dismiss a rollback review.")
        record = await self.repo.get_recommendation_by_id(rec_id)
        if record is None:
            raise RecommendationNotFoundError(f"Recommendation {rec_id} not found.")
        if not self._dismissable(record):
            raise RecommendationTransitionError(
                f"Recommendation {rec_id} is {_status_value(record.status)}; only rollback_review can be dismissed."
            )
        moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        siblings = await self._attribution_siblings(record)
        group_ids = _group_ids(record, siblings)

        async def dismiss(member: RecommendationRecord, via: Optional[int]) -> RecommendationRecord:
            assert member.id is not None
            updates, previous = self._fresh_measurement_updates(member, window_hours, moment, group_ids)
            return await self.transition(
                member.id,
                RecommendationStatus.APPLIED,
                event_type=RecommendationEventType.ROLLBACK_DISMISSED,
                updates={**updates, "verification_status": VerificationStatus.PENDING.value},
                actor=actor,
                payload={
                    "reason": reason.strip(),
                    "dismissed_measurement_id": previous,
                    **self._group_payload(group_ids, via),
                    **self._window_payload(updates),
                },
                expected_statuses=(RecommendationStatus.ROLLBACK_REVIEW,),
            )

        dismissed = await dismiss(record, None)
        await self._apply_to_siblings(siblings, self._dismissable, lambda member: dismiss(member, rec_id))
        return dismissed

    async def request_remeasure(
        self,
        rec_id: int,
        *,
        reason: Optional[str] = None,
        actor: str = "user",
        window_hours: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> RecommendationRecord:
        """Start a new fixed-horizon measurement of a ``not_realized``/``inconclusive`` change (C0.5).

        The previous measurement stays immutable; the next one supersedes it.
        Every member of the record's attribution group (C0.10) whose last
        measurement is also ``not_realized``/``inconclusive`` is re-measured
        with it over the same window; members that keep a decided measurement
        keep their share of the change's delta, so no member claims it twice.
        """
        record = await self.repo.get_recommendation_by_id(rec_id)
        if record is None:
            raise RecommendationNotFoundError(f"Recommendation {rec_id} not found.")
        if not self._remeasurable(record):
            raise RecommendationTransitionError(
                f"Recommendation {rec_id} is {_status_value(record.status)} with verification "
                f"{record.verification_status}; only not_realized or inconclusive measurements can be re-measured."
            )
        moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        siblings = await self._attribution_siblings(record)
        group_ids = _group_ids(record, siblings)

        async def remeasure(member: RecommendationRecord, via: Optional[int]) -> RecommendationRecord:
            assert member.id is not None
            updates, previous = self._fresh_measurement_updates(member, window_hours, moment, group_ids)
            return await self.transition(
                member.id,
                RecommendationStatus.VERIFYING,
                event_type=RecommendationEventType.REMEASURE_REQUESTED,
                updates={**updates, "verification_status": VerificationStatus.IN_PROGRESS.value},
                actor=actor,
                payload={
                    "reason": reason,
                    "previous_verification_status": member.verification_status,
                    "supersedes_measurement_id": previous,
                    **self._group_payload(group_ids, via),
                    **self._window_payload(updates),
                },
                expected_statuses=(RecommendationStatus.APPLIED,),
            )

        remeasured = await remeasure(record, None)
        await self._apply_to_siblings(siblings, self._remeasurable, lambda member: remeasure(member, rec_id))
        return remeasured

    @staticmethod
    def _dismissable(record: RecommendationRecord) -> bool:
        return _status_value(record.status) == RecommendationStatus.ROLLBACK_REVIEW.value

    @staticmethod
    def _remeasurable(record: RecommendationRecord) -> bool:
        return (
            _status_value(record.status) == RecommendationStatus.APPLIED.value
            and record.verification_status in REMEASURABLE_VERIFICATION_STATUSES
        )

    async def _attribution_siblings(self, record: RecommendationRecord) -> List[RecommendationRecord]:
        """The other members of the change ``record`` belongs to (its attribution group, C0.10)."""
        from greenkube.core.optimization.verifier import IN_EFFECT_STATUSES, attribution_groups

        if record.id is None:
            return []
        try:
            records = await self.repo.get_recommendations_by_statuses(
                list(IN_EFFECT_STATUSES), namespace=record.namespace
            )
        except Exception as exc:
            logger.warning("Could not read the attribution group of recommendation %s: %s", record.id, exc)
            return []
        candidates = [
            item for item in records or [] if item.id is not None and item.id != record.id and item.applied_at
        ]
        for group in attribution_groups([record, *candidates]):
            if any(member.id == record.id for member in group):
                return [member for member in group if member.id != record.id]
        return []

    @staticmethod
    async def _apply_to_siblings(
        siblings: List[RecommendationRecord],
        eligible: Callable[[RecommendationRecord], bool],
        action: Callable[[RecommendationRecord], Awaitable[RecommendationRecord]],
    ) -> None:
        """Apply a group action to the eligible siblings; a sibling that moved meanwhile is skipped."""
        for sibling in siblings:
            if not eligible(sibling):
                continue
            try:
                await action(sibling)
            except RecommendationTransitionError as exc:
                logger.warning("Attribution group member %s was not updated: %s", sibling.id, exc)

    @staticmethod
    def _group_payload(group_ids: List[int], via: Optional[int]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if len(group_ids) > 1:
            payload["attribution_group_ids"] = group_ids
        if via is not None:
            payload["requested_via"] = via
        return payload

    @staticmethod
    def _fresh_measurement_updates(
        record: RecommendationRecord,
        window_hours: Optional[float],
        now: Optional[datetime] = None,
        group_ids: Optional[List[int]] = None,
    ) -> tuple[dict[str, Any], Optional[str]]:
        if window_hours is None:
            from greenkube.core.config import get_config

            window_hours = float(getattr(get_config(), "VERIFICATION_WINDOW_HOURS", 72))
        now = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        baseline = _baseline_dict(record)
        measurement_ids = list(baseline.get("measurement_ids") or [])
        previous = baseline.get("latest_measurement_id") or (measurement_ids[-1] if measurement_ids else None)
        baseline["measurement_sequence"] = int(baseline.get("measurement_sequence") or 1) + 1
        baseline["verification_extended"] = False
        if group_ids and len(group_ids) > 1:
            # Sticky membership: the verifier measures the members together (BUG-36).
            baseline["attribution_group_ids"] = group_ids
        if "baseline_window_end" not in baseline and record.applied_at is not None:
            # Keep the original counterfactual: the baseline never moves past the change.
            baseline["baseline_window_end"] = ensure_utc(record.applied_at).isoformat()
        return (
            {
                "baseline": baseline,
                "verification_window_start": now,
                "verification_window_end": now + timedelta(hours=float(window_hours)),
                "verified_at": None,
                "measured_co2e_saved_grams": None,
                "measured_cost_saved": None,
                "savings_realized": None,
            },
            previous,
        )

    @staticmethod
    def _window_payload(updates: dict[str, Any]) -> dict[str, Optional[str]]:
        return {
            "verification_window_start": _iso(updates.get("verification_window_start")),
            "verification_window_end": _iso(updates.get("verification_window_end")),
        }

    async def mark_regressed(
        self,
        rec_id: int,
        *,
        reasons: List[str],
        actor: str = "system",
    ) -> RecommendationRecord:
        """Mark a realized saving as regressed (placeholder for the C8 DriftMonitor)."""
        return await self.transition(
            rec_id,
            RecommendationStatus.REGRESSED,
            event_type=RecommendationEventType.REGRESSED,
            updates={"savings_realized": False},
            actor=actor,
            payload={"reasons": reasons},
            expected_statuses=(RecommendationStatus.VERIFIED,),
        )

    async def mark_reverted(
        self,
        rec_id: int,
        *,
        rollback_plan_id: str,
        rollback_of_plan_id: str,
        actual_cpu_request_millicores: Optional[int],
        actual_memory_request_bytes: Optional[int],
        actor: str = "system",
    ) -> RecommendationRecord:
        """Mark a rollback complete after its synced per-container state is verified."""
        return await self.transition(
            rec_id,
            RecommendationStatus.REVERTED,
            event_type=RecommendationEventType.REVERTED,
            updates={
                "verified_at": datetime.now(timezone.utc),
                "verification_status": VerificationStatus.PASSED.value,
                "measured_co2e_saved_grams": None,
                "measured_cost_saved": None,
                "savings_realized": False,
            },
            actor=actor,
            payload={
                "rollback_plan_id": rollback_plan_id,
                "rollback_of_plan_id": rollback_of_plan_id,
                "actual_cpu_request_millicores": actual_cpu_request_millicores,
                "actual_memory_request_bytes": actual_memory_request_bytes,
            },
        )

    async def mark_inconclusive(
        self,
        rec_id: int,
        *,
        reason: str,
        verification_window_start: Optional[datetime] = None,
        verification_window_end: Optional[datetime] = None,
        baseline: Optional[dict] = None,
        details: Optional[dict] = None,
    ) -> RecommendationRecord:
        """Close a measurement without a decision; the measured input is kept (C0.13)."""
        updates: dict[str, Any] = {
            "verification_status": VerificationStatus.INCONCLUSIVE.value,
            "verification_window_start": verification_window_start,
            "verification_window_end": verification_window_end,
            "verified_at": None,
            "measured_co2e_saved_grams": None,
            "measured_cost_saved": None,
            "savings_realized": False,
        }
        if baseline is not None:
            updates["baseline"] = baseline
        return await self.transition(
            rec_id,
            RecommendationStatus.APPLIED,
            event_type=RecommendationEventType.INCONCLUSIVE,
            updates=updates,
            payload={"reason": reason, **(details or {})},
            expected_statuses=(RecommendationStatus.APPLIED, RecommendationStatus.VERIFYING),
        )

    async def mark_not_realized(
        self,
        rec_id: int,
        *,
        reasons: List[str],
        measured_co2e_saved_grams: Optional[float] = None,
        measured_cost_saved: Optional[float] = None,
        verification_window_start: Optional[datetime] = None,
        verification_window_end: Optional[datetime] = None,
        baseline: Optional[dict] = None,
        details: Optional[dict] = None,
    ) -> RecommendationRecord:
        """Keeps the record applied but flags that the measured saving was not realized.

        ``not_realized`` is terminal for this measurement: only an explicit
        re-measurement starts a new one (C0.5).
        """
        updates: dict[str, Any] = {
            "verification_status": VerificationStatus.FAILED.value,
            "verification_window_start": verification_window_start,
            "verification_window_end": verification_window_end,
            "measured_co2e_saved_grams": measured_co2e_saved_grams,
            "measured_cost_saved": measured_cost_saved,
            "savings_realized": False,
        }
        if baseline is not None:
            updates["baseline"] = baseline
        return await self.transition(
            rec_id,
            RecommendationStatus.APPLIED,
            event_type=RecommendationEventType.NOT_REALIZED,
            updates=updates,
            payload={"reasons": reasons, "gate": "savings", **(details or {})},
            expected_statuses=(RecommendationStatus.APPLIED, RecommendationStatus.VERIFYING),
        )
