"""Newer advice for an open plan (roadmap E7, J5).

A plan pins the revision of each recommendation it implements
(``recommendation_plan_pins``). Recommendations never change under an open
plan: a scan that computes different advice queues it as the next revision.
:func:`newer_revision_for_plan` reports, for one plan, every recommendation
whose latest advice (queued, or already promoted once the plan was closed) is
newer than the pinned revision. :func:`newer_advice_hook` turns it into the
"newer advice available" note of the plan's pull-request status comment
(:class:`~greenkube.automation.pr_feedback.PullRequestFeedbackPublisher`).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from greenkube.models.recommendation_v2 import NewerRevision, revision_snapshot

logger = logging.getLogger(__name__)


async def newer_revision_for_plan(plan_id: str, recommendation_repository: Any) -> List[NewerRevision]:
    """Recommendations of ``plan_id`` whose advice is newer than the revision the plan pinned.

    Plans created before revisions were pinned have no pins and report nothing.
    """
    pins = await recommendation_repository.get_plan_pins(plan_id)
    if not pins:
        return []
    queued = await recommendation_repository.get_queued_revisions(list(pins))
    newer: List[NewerRevision] = []
    for recommendation_id, pinned in sorted(pins.items()):
        waiting = queued.get(recommendation_id)
        if waiting is not None and waiting.revision > pinned:
            newer.append(
                NewerRevision(
                    plan_id=plan_id,
                    recommendation_id=recommendation_id,
                    pinned_revision=pinned,
                    latest_revision=waiting.revision,
                    queued=True,
                    snapshot=dict(waiting.snapshot),
                )
            )
            continue
        record = await recommendation_repository.get_recommendation_by_id(recommendation_id)
        if record is None or int(record.revision or 1) <= pinned:
            continue
        newer.append(
            NewerRevision(
                plan_id=plan_id,
                recommendation_id=recommendation_id,
                pinned_revision=pinned,
                latest_revision=int(record.revision),
                queued=False,
                snapshot=revision_snapshot(record),
            )
        )
    return newer


def describe_newer_advice(newer: Sequence[NewerRevision]) -> Optional[str]:
    """One line for the PR comment, or None when the plan implements the latest advice."""
    if not newer:
        return None
    return " ".join(item.describe() for item in newer) + " Open a new preview to apply it."


def newer_advice_hook(recommendation_repository: Any) -> Callable[[Any, Sequence[Any]], Awaitable[Optional[str]]]:
    """The ``NewerAdviceHook`` of the PR lifecycle comment (``automation.pr_feedback``)."""

    async def hook(plan: Any, recommendations: Sequence[Any]) -> Optional[str]:
        return describe_newer_advice(await newer_revision_for_plan(plan.plan_id, recommendation_repository))

    return hook
