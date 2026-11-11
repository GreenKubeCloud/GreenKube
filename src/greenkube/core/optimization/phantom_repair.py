"""Idempotent repair of phantom "detected" changes written by the old apply detector.

Before wave 3a, apply detection recorded a rightsizing as applied (method
``detected``) when the untouched live request lay within the apply tolerance of
the proposal, or when the matching rollout completed before the recommendation
existed. Those rows sit in the applied family with identical before and after
values, accrue projected savings and own a change measurement, although the
change never happened.

A row alone cannot tell such a phantom from a real detected change whose
``actual_*`` request an old rescan reverted to the baseline (migration ``0061``
left detected rows alone for that reason), so the repair needs evidence:

* the frozen baseline says the matching rollout completed before the
  recommendation was created (that rollout cannot be its application), or
* ``actual_*`` equals the baseline request, the proposal differs, and the
  newest rolled-up request of the container, observed after ``applied_at``,
  is still the baseline request.

A phantom moves to ``stale`` with a ``retired`` event (code
``phantom_apply_detection``) and its realized savings cleared; its savings
ledger rows are superseded and its change measurement is closed (``retired``)
when every member of the change is a phantom. Without evidence nothing is
changed. Running it again changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.utils.time import ensure_utc

if TYPE_CHECKING:
    from greenkube.storage.base_repository import RecommendationRepository
    from greenkube.storage.base_savings_repository import SavingsLedgerRepository
    from greenkube.storage.base_workload_usage_repository import WorkloadUsageRepository
    from greenkube.storage.change_repository import ChangeRepository

logger = logging.getLogger(__name__)

#: Statuses of a recorded change (the "applied family").
APPLIED_FAMILY = ("applied", "verifying", "verified", "rollback_review", "regressed")
#: Event code of a retired phantom.
PHANTOM_CODE = "phantom_apply_detection"
#: How far back the newest rolled-up request is read.
EVIDENCE_LOOKBACK = timedelta(hours=6)


@dataclass
class PhantomRepairResult:
    """What one repair run changed."""

    retired: List[int] = field(default_factory=list)
    closed_changes: List[str] = field(default_factory=list)
    superseded_ledger_rows: int = 0
    #: Candidates left alone for lack of evidence (no recent usage, or a moved request).
    unproven: List[int] = field(default_factory=list)


def _value(raw: Any) -> str:
    return str(getattr(raw, "value", raw) or "")


def _resource(record: RecommendationRecord) -> Optional[Tuple[Optional[int], Optional[int], Optional[int]]]:
    """``(current, recommended, actual)`` of the record's resource, or None for other types."""
    if record.type == RecommendationType.RIGHTSIZING_CPU:
        return (
            record.current_cpu_request_millicores,
            record.recommended_cpu_request_millicores,
            record.actual_cpu_request_millicores,
        )
    if record.type == RecommendationType.RIGHTSIZING_MEMORY:
        return (
            record.current_memory_request_bytes,
            record.recommended_memory_request_bytes,
            record.actual_memory_request_bytes,
        )
    return None


def _baseline(record: RecommendationRecord) -> Dict[str, Any]:
    baseline = record.baseline
    if baseline is None:
        return {}
    if hasattr(baseline, "model_dump"):
        return baseline.model_dump(mode="json")
    return dict(baseline) if isinstance(baseline, dict) else {}


def _parse(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value:
        try:
            return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def rollout_before_creation(record: RecommendationRecord) -> bool:
    """The frozen baseline's rollout completed before the recommendation existed."""
    rollout = _baseline(record).get("rollout")
    completed = _parse(rollout.get("completed_at")) if isinstance(rollout, dict) else None
    return completed is not None and record.created_at is not None and completed < ensure_utc(record.created_at)


def unchanged_candidate(record: RecommendationRecord) -> bool:
    """``actual_*`` equals the baseline request while the proposal differs."""
    values = _resource(record)
    if values is None:
        return False
    current, recommended, actual = values
    return current is not None and actual is not None and actual == current and recommended != current


def is_candidate(record: RecommendationRecord) -> bool:
    return (
        record.id is not None
        and _value(record.application_method) == "detected"
        and _value(record.status) in APPLIED_FAMILY
        and _resource(record) is not None
        and (rollout_before_creation(record) or unchanged_candidate(record))
    )


def _target(record: RecommendationRecord) -> Optional[Tuple[str, str, str]]:
    if not record.namespace:
        return None
    if record.owner_kind and record.owner_name:
        return (record.namespace, record.owner_kind, record.owner_name)
    if record.pod_name:
        return (record.namespace, "Pod", record.pod_name)
    return None


def _observed_request(record: RecommendationRecord, summary: Any) -> Optional[int]:
    if summary is None:
        return None
    value = summary.cpu_request if record.type == RecommendationType.RIGHTSIZING_CPU else summary.memory_request
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


class PhantomChangeRepair:
    """Retires phantom detected changes, with evidence, idempotently."""

    def __init__(
        self,
        recommendations: "RecommendationRepository",
        *,
        usage: Optional["WorkloadUsageRepository"] = None,
        savings: Optional["SavingsLedgerRepository"] = None,
        changes: Optional["ChangeRepository"] = None,
    ) -> None:
        self.recommendations = recommendations
        self.usage = usage
        self.savings = savings
        self.changes = changes

    async def run(self, now: Optional[datetime] = None) -> PhantomRepairResult:
        moment = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
        result = PhantomRepairResult()
        records = await self.recommendations.get_recommendations_by_statuses(list(APPLIED_FAMILY))
        candidates = [record for record in records or [] if is_candidate(record)]
        if not candidates:
            return result
        observed = await self._observed(candidates, moment)
        phantoms: List[Tuple[RecommendationRecord, Dict[str, Any]]] = []
        for record in candidates:
            evidence = self._evidence(record, observed)
            if evidence is None:
                result.unproven.append(int(record.id or 0))
            else:
                phantoms.append((record, evidence))
        for record, evidence in phantoms:
            if await self._retire(record, evidence):
                result.retired.append(int(record.id or 0))
        await self._settle(result)
        if result.retired:
            logger.warning(
                "Retired %d phantom detected change(s) (%s): the live request never moved to the proposal.",
                len(result.retired),
                ", ".join(str(item) for item in result.retired),
            )
        return result

    async def _observed(
        self, candidates: Sequence[RecommendationRecord], now: datetime
    ) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        """Newest rolled-up summaries of the candidates' targets, by namespace."""
        if self.usage is None:
            return {}
        summaries: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        namespaces = sorted({record.namespace for record in candidates if record.namespace})
        for namespace in namespaces:
            try:
                snapshot = await self.usage.read_usage_snapshot(
                    now - EVIDENCE_LOOKBACK, now + timedelta(hours=1), namespace=namespace
                )
            except Exception as exc:
                logger.warning("Phantom repair could not read the usage of namespace %s: %s", namespace, exc)
                continue
            summaries.update(snapshot.summaries)
        return summaries

    def _evidence(
        self, record: RecommendationRecord, observed: Dict[Tuple[str, str, str], Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if rollout_before_creation(record):
            rollout = _baseline(record).get("rollout") or {}
            return {"evidence": "rollout_before_recommendation", "rollout_completed_at": rollout.get("completed_at")}
        target = _target(record)
        values = _resource(record)
        if target is None or values is None or record.applied_at is None:
            return None
        current, recommended, _ = values
        summary = observed.get(target, {}).get(record.container_name or "")
        live = _observed_request(record, summary)
        last_hour = getattr(summary, "last_hour", None)
        # The newest request must be observed in an hour that starts after the change was recorded.
        if live is None or last_hour is None or ensure_utc(last_hour) <= ensure_utc(record.applied_at):
            return None
        if live != current or live == recommended:
            return None
        return {"evidence": "live_request_unchanged", "observed_request": live, "observed_hour": last_hour.isoformat()}

    async def _retire(self, record: RecommendationRecord, evidence: Dict[str, Any]) -> bool:
        status = _value(record.status)
        updates = {
            "status": "stale",
            "carbon_saved_co2e_grams": None,
            "cost_saved": None,
            "measured_co2e_saved_grams": None,
            "measured_cost_saved": None,
            "savings_realized": None,
        }
        payload = {
            "code": PHANTOM_CODE,
            "from_status": status,
            "to_status": "stale",
            "reason": (
                "Retired: apply detection recorded this change although the live request never moved from "
                "the baseline to the proposal (or the rollout predates the recommendation). Its savings and "
                "measurement are withdrawn."
            ),
            **evidence,
        }
        transition = getattr(self.recommendations, "transition_and_record_event", None)
        if transition is None:
            # Only the SQL repositories update and append the event atomically, with a compare-and-swap.
            logger.warning("Phantom repair needs an atomic recommendation transition; %s left as is.", record.id)
            return False
        try:
            updated = await transition(
                int(record.id or 0), updates, "retired", "system", payload, expected_statuses=[status]
            )
        except Exception as exc:
            logger.warning("Could not retire phantom change %s: %s", record.id, exc)
            return False
        return updated is not None

    async def _settle(self, result: PhantomRepairResult) -> None:
        """Supersede the retired rows' ledger and close changes made only of phantoms."""
        if not result.retired:
            return
        if self.savings is not None:
            for rec_id in result.retired:
                try:
                    result.superseded_ledger_rows += int(await self.savings.supersede_for_recommendation(rec_id) or 0)
                except Exception as exc:
                    logger.warning("Could not supersede the savings of phantom change %s: %s", rec_id, exc)
        if self.changes is not None:
            await self._close_changes(result)

    async def _close_changes(self, result: PhantomRepairResult) -> None:
        from greenkube.verify.model import MeasurementState

        assert self.changes is not None
        try:
            by_recommendation = await self.changes.change_ids_for_recommendations(result.retired)
        except Exception as exc:
            logger.warning("Could not read the changes of phantom recommendations: %s", exc)
            return
        for change_id in sorted(set(by_recommendation.values())):
            try:
                change = await self.changes.get_change(change_id)
                status = await self.changes.get_status(change_id)
                if change is None or status is None or status.state is MeasurementState.CLOSED:
                    continue
                if not await self._only_phantoms(change.recommendation_ids, result.retired):
                    continue
                await self.changes.transition(
                    status,
                    MeasurementState.CLOSED,
                    event_type="retired",
                    reason=f"{PHANTOM_CODE}: every member was a phantom detected change",
                    payload={"code": PHANTOM_CODE, "recommendation_ids": sorted(change.recommendation_ids)},
                )
                result.closed_changes.append(change_id)
            except Exception as exc:
                logger.warning("Could not close the measurement of phantom change %s: %s", change_id, exc)

    async def _only_phantoms(self, members: Iterable[int], retired: Sequence[int]) -> bool:
        for member in members:
            if member in retired:
                continue
            record = await self.recommendations.get_recommendation_by_id(member)
            if record is None:
                continue
            if _value(record.status) in APPLIED_FAMILY:
                return False
        return True


__all__ = ["PHANTOM_CODE", "PhantomChangeRepair", "PhantomRepairResult", "is_candidate"]
