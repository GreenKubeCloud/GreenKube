# src/greenkube/core/optimization/lifecycle.py
"""Recommendation lifecycle state machine and audit-trail emission.

Every transition goes through :class:`RecommendationLifecycle` so that the
``recommendation_events`` audit trail is always written alongside the status
change. Apply detection and verification jobs use the same service.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from greenkube.models.metrics import (
    ApplicationMethod,
    RecommendationEvent,
    RecommendationEventType,
    RecommendationRecord,
    RecommendationStatus,
    VerificationStatus,
)

if TYPE_CHECKING:
    from greenkube.storage.base_repository import RecommendationRepository

logger = logging.getLogger(__name__)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def freeze_baseline(record: RecommendationRecord) -> dict:
    """Captures the pre-apply evidence snapshot used as the verification baseline."""
    evidence = record.evidence
    baseline: dict = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "current_cpu_request_millicores": record.current_cpu_request_millicores,
        "current_memory_request_bytes": record.current_memory_request_bytes,
        "recommended_cpu_request_millicores": record.recommended_cpu_request_millicores,
        "recommended_memory_request_bytes": record.recommended_memory_request_bytes,
        "potential_savings_cost": record.potential_savings_cost,
        "potential_savings_co2e_grams": record.potential_savings_co2e_grams,
    }
    if evidence is not None:
        baseline.update(
            {
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
        """Persists one audit event, tolerating repository failures."""
        value = event_type.value if hasattr(event_type, "value") else str(event_type)
        event = RecommendationEvent(
            recommendation_id=rec_id,
            event_type=value,
            actor=actor,
            payload=payload or {},
        )
        try:
            return await self.repo.record_event(event)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not record recommendation event %s for %s: %s", value, rec_id, exc)
            return event

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
    ) -> RecommendationRecord:
        """Updates a record's status/fields and optionally emits its audit event."""
        status_value = status.value if hasattr(status, "value") else str(status)
        changes = dict(updates or {})
        changes["status"] = status_value
        changes.setdefault("updated_at", datetime.now(timezone.utc))
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
    ) -> RecommendationRecord:
        """Marks a recommendation applied, freezing its verification baseline."""
        from greenkube.models.metrics import ApplyRecommendationRequest

        record = await self.repo.get_recommendation_by_id(rec_id)
        if record is None:
            raise ValueError(f"Recommendation {rec_id} not found.")

        baseline = freeze_baseline(record)
        request = ApplyRecommendationRequest(
            actual_cpu_request_millicores=actual_cpu,
            actual_memory_request_bytes=actual_memory,
            carbon_saved_co2e_grams=carbon_saved_co2e_grams,
            cost_saved=cost_saved,
            application_method=application_method,
        )
        updated = await self.repo.apply_recommendation(rec_id, request, baseline=baseline)
        await self.record_event(
            rec_id,
            RecommendationEventType.APPLIED,
            actor=actor,
            payload={"application_method": application_method},
        )
        return updated

    async def expire(self, now: Optional[datetime] = None) -> List[RecommendationRecord]:
        """Marks TTL-elapsed active recommendations as expired and records events."""
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
    ) -> RecommendationRecord:
        return await self.transition(
            rec_id,
            RecommendationStatus.VERIFIED,
            event_type=RecommendationEventType.VERIFIED,
            updates={
                "verified_at": datetime.now(timezone.utc),
                "verification_status": VerificationStatus.PASSED.value,
                "verification_window_start": verification_window_start,
                "verification_window_end": verification_window_end,
                "measured_co2e_saved_grams": measured_co2e_saved_grams,
                "measured_cost_saved": measured_cost_saved,
                "savings_realized": True,
            },
            payload={
                "measured_co2e_saved_grams": measured_co2e_saved_grams,
                "measured_cost_saved": measured_cost_saved,
            },
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
    ) -> RecommendationRecord:
        return await self.transition(
            rec_id,
            RecommendationStatus.ROLLBACK_REVIEW,
            event_type=RecommendationEventType.ROLLBACK_REVIEW,
            updates={
                "verification_status": VerificationStatus.FAILED.value,
                "verification_window_start": verification_window_start,
                "verification_window_end": verification_window_end,
                "measured_co2e_saved_grams": measured_co2e_saved_grams,
                "measured_cost_saved": measured_cost_saved,
                "savings_realized": False,
            },
            payload={"reasons": reasons},
        )

    async def mark_inconclusive(
        self,
        rec_id: int,
        *,
        reason: str,
        verification_window_start: Optional[datetime] = None,
        verification_window_end: Optional[datetime] = None,
    ) -> RecommendationRecord:
        return await self.transition(
            rec_id,
            RecommendationStatus.APPLIED,
            event_type=RecommendationEventType.VERIFIED,
            updates={
                "verification_status": VerificationStatus.INCONCLUSIVE.value,
                "verification_window_start": verification_window_start,
                "verification_window_end": verification_window_end,
                "savings_realized": False,
            },
            payload={"reason": reason},
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
    ) -> RecommendationRecord:
        """Keeps the record applied but flags that the cost/carbon gate failed."""
        return await self.transition(
            rec_id,
            RecommendationStatus.APPLIED,
            event_type=RecommendationEventType.FAILED,
            updates={
                "verification_status": VerificationStatus.FAILED.value,
                "verification_window_start": verification_window_start,
                "verification_window_end": verification_window_end,
                "measured_co2e_saved_grams": measured_co2e_saved_grams,
                "measured_cost_saved": measured_cost_saved,
                "savings_realized": False,
            },
            payload={"reasons": reasons, "gate": "savings"},
        )
