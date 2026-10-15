"""Reconcile deduplicated pull-request close and merge events into plan state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast

from greenkube.automation.git.base import PullRequestInfo
from greenkube.core.observability import record_pull_request_merge_detection
from greenkube.gitops.contracts import ActionStatus, PlanStatus, PullRequestEvent


class PullRequestLifecycleRecord(Protocol):
    """Fields needed to correlate and update one persisted recommendation PR."""

    @property
    def id(self) -> int | None: ...

    @property
    def recommendation_id(self) -> int: ...

    @property
    def provider_id(self) -> str | None: ...

    @property
    def repo(self) -> str: ...

    @property
    def pr_number(self) -> int | None: ...

    @property
    def plan_id(self) -> str | None: ...

    @property
    def status(self) -> str: ...


class PullRequestLifecyclePlan(Protocol):
    """Plan state used while reconciling one linked PR."""

    @property
    def plan_id(self) -> str: ...

    @property
    def digest(self) -> str | None: ...

    @property
    def recommendation_ids(self) -> Sequence[int]: ...

    @property
    def status(self) -> PlanStatus: ...


class PullRequestLifecycleRepository(Protocol):
    """PR-record persistence operations used by the lifecycle reconciler."""

    async def list_pull_requests_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> Sequence[PullRequestLifecycleRecord]: ...

    async def get_pull_request_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> PullRequestLifecycleRecord | None: ...

    async def get_pull_request_by_plan_id(self, plan_id: str) -> PullRequestLifecycleRecord | None: ...

    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict[str, Any],
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ) -> PullRequestLifecycleRecord: ...


class PullRequestLifecyclePlanRepository(Protocol):
    """Plan and recommendation action persistence operations used by reconciliation."""

    async def get_plan(self, plan_id: str) -> PullRequestLifecyclePlan | None: ...

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> PullRequestLifecyclePlan | None: ...

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None: ...

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int: ...


class PullRequestLifecycleGitProvider(Protocol):
    """Refetch one PR from the provider selected by its configured provider ID."""

    async def get_pull_request(self, provider_id: str, repo: str, number: int) -> PullRequestInfo | None: ...


class PullRequestLifecycleOutcome(StrEnum):
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    UNLINKED = "unlinked"
    MISMATCHED = "mismatched"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class PullRequestLifecycleResult:
    """Safe outcome of reconciling one deduplicated provider event."""

    outcome: PullRequestLifecycleOutcome
    plan_id: str | None = None


@dataclass(frozen=True, slots=True)
class _VerifiedProviderState:
    merged: bool
    merge_commit_sha: str | None
    merged_at: datetime | None
    closed_at: datetime | None


_SUPPORTED_ACTIONS = frozenset({"opened", "synchronized", "closed", "reopened"})
_PULL_REQUEST_STATUSES = frozenset({"pending", "open", "merged", "closed", "error"})
_PLAN_IN_FLIGHT_STATUSES = frozenset({PlanStatus.APPLYING, PlanStatus.PR_OPEN})
_PLAN_ADVANCED_STATUSES = frozenset({PlanStatus.SYNCED, PlanStatus.APPLIED, PlanStatus.SYNC_FAILED})
_PLAN_TERMINAL_FAILURE_STATUSES = frozenset(
    {PlanStatus.FAILED, PlanStatus.STALE, PlanStatus.SUPERSEDED, PlanStatus.REJECTED}
)
_ACTION_PRE_MERGE_STATUSES = frozenset(
    {ActionStatus.NONE, ActionStatus.GROUPED, ActionStatus.PLAN_READY, ActionStatus.PR_OPEN, ActionStatus.FAILED}
)
_ACTION_ADVANCED_STATUSES = frozenset({ActionStatus.SYNC_PENDING, ActionStatus.APPLIED})


class PullRequestLifecycleReconciler:
    """Apply verified merge/close state from webhooks or provider polling."""

    def __init__(
        self,
        *,
        pull_request_repository: PullRequestLifecycleRepository,
        plan_repository: PullRequestLifecyclePlanRepository,
        git_provider: PullRequestLifecycleGitProvider,
    ) -> None:
        self.pull_request_repository = pull_request_repository
        self.plan_repository = plan_repository
        self.git_provider = git_provider

    async def reconcile(
        self,
        event: PullRequestEvent,
        *,
        newly_deduplicated: bool,
    ) -> PullRequestLifecycleResult:
        return await self._reconcile_event(event, newly_deduplicated=newly_deduplicated)

    async def reconcile_polled(
        self,
        provider_id: str,
        repo: str,
        number: int,
    ) -> PullRequestLifecycleResult:
        """Fetch current provider state and reconcile a terminal linked PR."""
        if not _valid_token(provider_id) or not _valid_repository(repo) or type(number) is not int or number <= 0:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED)

        event = PullRequestEvent(
            provider_id=provider_id,
            delivery_id=f"poll-{number}",
            repo=repo,
            number=number,
            action="closed",
            merged=False,
            occurred_at=datetime.now(UTC),
        )
        return await self._reconcile_event(
            event,
            newly_deduplicated=True,
            poll_provider_state=True,
        )

    async def _reconcile_event(
        self,
        event: PullRequestEvent,
        *,
        newly_deduplicated: bool,
        poll_provider_state: bool = False,
    ) -> PullRequestLifecycleResult:
        if type(newly_deduplicated) is not bool:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED)
        if not newly_deduplicated:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.DUPLICATE)
        if not _valid_event(event):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED)
        if event.action != "closed":
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.IGNORED)

        linked_records = list(
            await self.pull_request_repository.list_pull_requests_by_provider_reference(
                event.provider_id,
                event.repo,
                event.number,
            )
        )
        if not linked_records:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.UNLINKED)
        if any(not _record_matches_event(record, event) for record in linked_records):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED)

        plan_ids = [getattr(record, "plan_id", None) for record in linked_records]
        if all(plan_id is None for plan_id in plan_ids):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.UNLINKED)
        if any(not isinstance(plan_id, str) for plan_id in plan_ids) or len(set(plan_ids)) != 1:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED)
        plan_id = plan_ids[0]
        if not isinstance(plan_id, str) or not _valid_token(plan_id):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED)

        plan_record = await self.pull_request_repository.get_pull_request_by_plan_id(plan_id)
        if (
            plan_record is None
            or not _record_matches_event(plan_record, event)
            or getattr(plan_record, "plan_id", None) != plan_id
        ):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        records = list(linked_records)
        if all(getattr(record, "id", None) != getattr(plan_record, "id", None) for record in records):
            records.append(plan_record)
        if not _same_pull_request_reference(records, plan_id):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

        plan = await self.plan_repository.get_plan(plan_id)
        if plan is None:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.UNLINKED, plan_id)
        if getattr(plan, "plan_id", None) != plan_id:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        recommendation_ids = _recommendation_ids(getattr(plan, "recommendation_ids", None))
        if recommendation_ids is None or any(
            getattr(record, "recommendation_id", None) not in recommendation_ids for record in records
        ):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        if {getattr(record, "recommendation_id", None) for record in records} != set(recommendation_ids):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

        current = await self.git_provider.get_pull_request(event.provider_id, event.repo, event.number)
        if poll_provider_state:
            if (
                current is None
                or type(current.number) is not int
                or current.number != event.number
                or not isinstance(current.status, str)
                or current.status != current.status.strip()
                or type(current.merged) is not bool
            ):
                return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

            status = current.status.casefold()
            if status == "open" and not current.merged:
                return PullRequestLifecycleResult(PullRequestLifecycleOutcome.IGNORED, plan_id)
            if status not in {"closed", "merged"}:
                return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

            raw = current.raw if isinstance(current.raw, Mapping) else {}
            raw_merge_sha = raw.get("merge_commit_sha")
            event = PullRequestEvent(
                provider_id=event.provider_id,
                delivery_id=event.delivery_id,
                repo=event.repo,
                number=event.number,
                action="closed",
                merged=current.merged,
                merge_commit_sha=raw_merge_sha if isinstance(raw_merge_sha, str) else None,
                occurred_at=datetime.now(UTC),
            )
        verified = _verify_provider_state(current, event)
        if verified is None:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

        merge_sha_missing = verified.merged and verified.merge_commit_sha is None
        target_plan_status = (
            PlanStatus.SYNC_FAILED if merge_sha_missing else PlanStatus.MERGED if verified.merged else PlanStatus.CLOSED
        )
        target_action_status = (
            ActionStatus.FAILED
            if merge_sha_missing
            else ActionStatus.MERGED
            if verified.merged
            else ActionStatus.CLOSED
        )
        outcome = PullRequestLifecycleOutcome.MERGED if verified.merged else PullRequestLifecycleOutcome.CLOSED
        try:
            current_plan_status = PlanStatus(getattr(plan, "status", None))
        except (TypeError, ValueError):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        if current_plan_status in _PLAN_TERMINAL_FAILURE_STATUSES:
            await self._retire_terminal_plan_pull_requests(records, plan_id, verified)
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

        record_statuses = [_record_status(record) for record in records]
        if any(status is None for status in record_statuses):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        if not verified.merged and (
            "merged" in record_statuses
            or current_plan_status is PlanStatus.MERGED
            or current_plan_status in _PLAN_ADVANCED_STATUSES
        ):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        if _stored_merge_metadata_conflicts(records, verified):
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

        transition_plan = _plan_transition_allowed(current_plan_status, target_plan_status)
        if not transition_plan and current_plan_status not in _PLAN_ADVANCED_STATUSES:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        action_updates = await self._action_updates(
            recommendation_ids,
            target_action_status,
            allow_transition=transition_plan,
        )
        if action_updates is None:
            return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

        if transition_plan and current_plan_status is not target_plan_status:
            updated_plan = await self.plan_repository.set_plan_status(
                plan_id,
                target_plan_status,
                expected_statuses=(current_plan_status,),
                expected_digest=plan.digest,
                actor="git-provider",
                reason_code=(
                    "merge_commit_sha_missing"
                    if merge_sha_missing
                    else "pull_request_merged"
                    if verified.merged
                    else "pull_request_closed"
                ),
            )
            try:
                updated_status = PlanStatus(getattr(updated_plan, "status", None))
            except (TypeError, ValueError):
                updated_status = None
            if updated_plan is None or updated_status is not target_plan_status:
                return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
        if action_updates:
            updated_count = await self.plan_repository.set_recommendation_action_status(
                action_updates,
                target_action_status,
                expected_statuses=tuple(
                    _ACTION_PRE_MERGE_STATUSES
                    | ({ActionStatus.CLOSED} if target_action_status is ActionStatus.MERGED else set())
                ),
                actor="git-provider",
                plan_id=plan_id,
            )
            if updated_count != len(action_updates):
                return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)

        updates: dict[str, object] = {"status": "merged" if verified.merged else "closed"}
        if merge_sha_missing:
            updates.update(
                {
                    "sync_status": "failed",
                    "sync_error_code": "merge_commit_sha_missing",
                }
            )
        if verified.merged:
            if verified.merge_commit_sha is not None:
                updates["merge_commit_sha"] = verified.merge_commit_sha
            if verified.merged_at is not None:
                updates["merged_at"] = verified.merged_at
            if verified.closed_at is not None:
                updates["closed_at"] = verified.closed_at
        elif verified.closed_at is not None:
            updates["closed_at"] = verified.closed_at

        for record in records:
            record_id = record.id
            if type(record_id) is not int or record_id <= 0:
                return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
            if not _record_needs_update(record, updates):
                continue
            expected_record_status = _record_status(record)
            if expected_record_status is None:
                return PullRequestLifecycleResult(PullRequestLifecycleOutcome.MISMATCHED, plan_id)
            await self.pull_request_repository.update_pull_request(
                record_id,
                updates.copy(),
                expected_statuses=(expected_record_status,),
                expected_plan_id=plan_id,
            )

        provider_merge_time = None
        if current is not None and isinstance(current.raw, Mapping):
            provider_merge_time = _parse_datetime(current.raw.get("merged_at"))
        if verified.merged and provider_merge_time is not None:
            record_pull_request_merge_detection((datetime.now(UTC) - provider_merge_time).total_seconds())

        return PullRequestLifecycleResult(outcome, plan_id)

    async def _retire_terminal_plan_pull_requests(
        self,
        records: Sequence[PullRequestLifecycleRecord],
        plan_id: str,
        verified: _VerifiedProviderState,
    ) -> None:
        status = "merged" if verified.merged else "closed"
        error_code = "terminal_plan_merged" if verified.merged else "terminal_plan_closed"
        updates: dict[str, Any] = {
            "status": status,
            "sync_status": "failed",
            "sync_error_code": error_code,
        }
        if verified.merge_commit_sha is not None:
            updates["merge_commit_sha"] = verified.merge_commit_sha
        if verified.merged_at is not None:
            updates["merged_at"] = verified.merged_at
        if verified.closed_at is not None:
            updates["closed_at"] = verified.closed_at

        for record in records:
            record_id = getattr(record, "id", None)
            current_status = _record_status(record)
            if type(record_id) is not int or record_id <= 0 or current_status is None:
                raise RuntimeError("terminal plan pull-request record is missing its current identity")
            if not _record_needs_update(record, updates):
                continue
            await self.pull_request_repository.update_pull_request(
                record_id,
                updates.copy(),
                expected_statuses=(current_status,),
                expected_plan_id=plan_id,
            )

    async def _action_updates(
        self,
        recommendation_ids: Sequence[int],
        target_status: ActionStatus,
        *,
        allow_transition: bool,
    ) -> list[int] | None:
        updates: list[int] = []
        for recommendation_id in recommendation_ids:
            current_status = await self.plan_repository.get_recommendation_action_status(recommendation_id)
            if current_status is None:
                return None
            try:
                current_status = ActionStatus(current_status)
            except (TypeError, ValueError):
                return None
            if current_status is target_status:
                continue
            if not allow_transition:
                continue
            if current_status in _ACTION_PRE_MERGE_STATUSES:
                updates.append(recommendation_id)
            elif target_status is ActionStatus.MERGED and current_status is ActionStatus.CLOSED:
                updates.append(recommendation_id)
            elif target_status is ActionStatus.MERGED and current_status in _ACTION_ADVANCED_STATUSES | {
                ActionStatus.MERGED
            }:
                continue
            elif target_status is ActionStatus.CLOSED and current_status in {
                ActionStatus.MERGED,
                ActionStatus.SYNC_PENDING,
                ActionStatus.APPLIED,
            }:
                return None
            else:
                return None
        return updates


def _valid_event(event: PullRequestEvent) -> bool:
    if not isinstance(event, PullRequestEvent):
        return False
    return (
        _valid_token(event.provider_id)
        and _valid_token(event.delivery_id)
        and _valid_repository(event.repo)
        and type(event.number) is int
        and event.number > 0
        and isinstance(event.action, str)
        and event.action in _SUPPORTED_ACTIONS
        and type(event.merged) is bool
        and _valid_optional_text(event.merge_commit_sha)
        and _valid_optional_text(event.head_sha)
        and _valid_optional_text(event.base_sha)
        and _utc_datetime(event.occurred_at) is not None
    )


def _valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _valid_repository(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip() or not value:
        return False
    if (
        value.startswith("/")
        or value.endswith("/")
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    parts = value.split("/")
    return len(parts) >= 2 and all(part not in {"", ".", ".."} for part in parts)


def _valid_optional_text(value: object) -> bool:
    return value is None or _valid_token(value)


def _record_matches_event(record: PullRequestLifecycleRecord, event: PullRequestEvent) -> bool:
    record_id = getattr(record, "id", None)
    recommendation_id = getattr(record, "recommendation_id", None)
    return (
        getattr(record, "provider_id", None) == event.provider_id
        and getattr(record, "repo", None) == event.repo
        and type(getattr(record, "pr_number", None)) is int
        and getattr(record, "pr_number", None) == event.number
        and type(record_id) is int
        and record_id > 0
        and type(recommendation_id) is int
        and recommendation_id > 0
    )


def _same_pull_request_reference(
    records: Sequence[PullRequestLifecycleRecord],
    plan_id: str,
) -> bool:
    record_ids = [getattr(record, "id", None) for record in records]
    recommendation_ids = [getattr(record, "recommendation_id", None) for record in records]
    return (
        all(type(record_id) is int for record_id in record_ids)
        and len(record_ids) == len(set(record_ids))
        and all(type(recommendation_id) is int and recommendation_id > 0 for recommendation_id in recommendation_ids)
        and all(getattr(record, "plan_id", None) == plan_id for record in records)
    )


def _recommendation_ids(values: object) -> list[int] | None:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    ids = list(values)
    if not ids or any(type(value) is not int or value <= 0 for value in ids) or len(ids) != len(set(ids)):
        return None
    return cast(list[int], ids)


def _record_status(record: PullRequestLifecycleRecord) -> str | None:
    status = getattr(record, "status", None)
    value = getattr(status, "value", status)
    if not isinstance(value, str):
        return None
    value = value.casefold()
    return value if value in _PULL_REQUEST_STATUSES else None


def _plan_transition_allowed(current: PlanStatus, target: PlanStatus) -> bool:
    if current is target or current in _PLAN_IN_FLIGHT_STATUSES:
        return True
    return target is PlanStatus.MERGED and current is PlanStatus.CLOSED


def _stored_merge_metadata_conflicts(
    records: Sequence[PullRequestLifecycleRecord],
    verified: _VerifiedProviderState,
) -> bool:
    for record in records:
        stored_sha = getattr(record, "merge_commit_sha", None)
        if stored_sha is not None:
            if not verified.merged or not _valid_optional_text(stored_sha):
                return True
            if verified.merge_commit_sha is not None and stored_sha.casefold() != verified.merge_commit_sha.casefold():
                return True
        stored_merged_at = getattr(record, "merged_at", None)
        if stored_merged_at is not None:
            if not verified.merged:
                return True
            parsed = _parse_datetime(stored_merged_at)
            if parsed is None:
                return True
            if verified.merged_at is not None and parsed != verified.merged_at:
                return True
    return False


def _record_needs_update(record: PullRequestLifecycleRecord, updates: Mapping[str, object]) -> bool:
    for field, value in updates.items():
        current = getattr(record, field, None)
        if field == "status":
            current = getattr(current, "value", current)
        if current != value:
            return True
    return False


def _verify_provider_state(
    current: PullRequestInfo | None,
    event: PullRequestEvent,
) -> _VerifiedProviderState | None:
    if current is None or type(current.number) is not int or current.number != event.number:
        return None
    if not isinstance(current.status, str) or current.status != current.status.strip():
        return None
    if type(current.merged) is not bool or not isinstance(current.raw, Mapping):
        return None

    status = current.status.casefold()
    raw = current.raw
    for field in ("number", "iid"):
        value = raw.get(field)
        if value is not None and (type(value) is not int or value != event.number):
            return None
    raw_status = raw.get("state")
    if raw_status is not None and (not isinstance(raw_status, str) or raw_status.casefold() != status):
        return None
    if not _raw_repository_matches(raw, event.repo):
        return None

    raw_merged = raw.get("merged")
    if raw_merged is not None and type(raw_merged) is not bool:
        return None
    merged_at = _parse_datetime(raw.get("merged_at"))
    if raw.get("merged_at") is not None and merged_at is None:
        return None
    closed_at = _parse_datetime(raw.get("closed_at"))
    if raw.get("closed_at") is not None and closed_at is None:
        return None
    raw_merge_sha = raw.get("merge_commit_sha")
    if not _valid_optional_text(raw_merge_sha):
        return None
    raw_merge_sha = raw_merge_sha if isinstance(raw_merge_sha, str) else None

    state_is_merged = current.merged
    if (
        (raw_merged is not None and raw_merged is not state_is_merged)
        or (merged_at is not None and not state_is_merged)
        or (status == "merged" and not state_is_merged)
        or (state_is_merged and status not in {"closed", "merged"})
        or (not state_is_merged and status != "closed")
        or event.merged is not state_is_merged
    ):
        return None

    if state_is_merged:
        if (
            event.merge_commit_sha is not None
            and raw_merge_sha is not None
            and event.merge_commit_sha.casefold() != raw_merge_sha.casefold()
        ):
            return None
        merge_commit_sha = raw_merge_sha or event.merge_commit_sha
        occurred_at = _utc_datetime(event.occurred_at)
        if occurred_at is None:
            return None
        return _VerifiedProviderState(
            merged=True,
            merge_commit_sha=merge_commit_sha,
            merged_at=merged_at or occurred_at,
            closed_at=closed_at,
        )

    if raw_merge_sha is not None or event.merge_commit_sha is not None:
        return None
    occurred_at = _utc_datetime(event.occurred_at)
    if occurred_at is None:
        return None
    return _VerifiedProviderState(
        merged=False,
        merge_commit_sha=None,
        merged_at=None,
        closed_at=closed_at or occurred_at,
    )


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc_datetime(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc_datetime(parsed)


def _raw_repository_matches(raw: Mapping[str, object], expected_repo: str) -> bool:
    repository = raw.get("repository")
    if repository is not None:
        if not isinstance(repository, Mapping):
            return False
        full_name = repository.get("full_name")
        if full_name is not None and full_name != expected_repo:
            return False

    base = raw.get("base")
    if base is not None:
        if not isinstance(base, Mapping):
            return False
        base_repository = base.get("repo")
        if base_repository is not None:
            if not isinstance(base_repository, Mapping):
                return False
            full_name = base_repository.get("full_name")
            if full_name is not None and full_name != expected_repo:
                return False
    return True


def _utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
