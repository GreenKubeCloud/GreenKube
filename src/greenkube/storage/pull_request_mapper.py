# src/greenkube/storage/pull_request_mapper.py
"""Shared mapping between pull-request rows and PullRequestRecord."""

from datetime import datetime, timezone
from typing import Optional

from greenkube.models.metrics import PullRequestRecord, PullRequestStatus

#: Columns a lifecycle update may touch.
MUTABLE_PR_COLUMNS: frozenset = frozenset(
    {"provider", "repo", "base_branch", "head_branch", "pr_number", "pr_url", "status", "error", "updated_at"}
)


def _as_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _as_status(value) -> PullRequestStatus:
    try:
        return PullRequestStatus(value)
    except ValueError:
        return PullRequestStatus.PENDING


def _get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def row_to_pull_request(row) -> PullRequestRecord:
    """Converts a database row to a PullRequestRecord."""
    return PullRequestRecord(
        id=row["id"],
        recommendation_id=row["recommendation_id"],
        provider=str(_get(row, "provider", "github") or "github"),
        repo=str(row["repo"]),
        base_branch=str(_get(row, "base_branch", "main") or "main"),
        head_branch=_get(row, "head_branch"),
        pr_number=_get(row, "pr_number"),
        pr_url=_get(row, "pr_url"),
        status=_as_status(_get(row, "status", "pending")),
        error=_get(row, "error"),
        created_at=_as_datetime(_get(row, "created_at")) or datetime.now(timezone.utc),
        updated_at=_as_datetime(_get(row, "updated_at")),
    )
