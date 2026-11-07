# src/greenkube/api/startup.py
"""
Background tasks started by the API lifespan after the DB connection is established.

These tasks run as fire-and-forget background coroutines so they never
block application startup. Each task must catch its own exceptions; a
failure must not prevent the API from serving requests.

The API does not run recommendation scans: they are scheduled and executed by
the leader controller (``greenkube.core.optimization.scan_service``). API
replicas only enqueue runs through ``POST /api/v1/optimization/runs``.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from greenkube.gitops.contracts import Plan

logger = logging.getLogger(__name__)
PLAN_HISTORY_RETENTION_DAYS = 90


class DuePlanExpirer(Protocol):
    async def expire_due_plans(self) -> Sequence[Plan]: ...

    async def prune_retained_history(self, *, cutoff: datetime) -> dict[str, int]: ...

    async def reconcile_exhausted_operations(self) -> Sequence[Plan]: ...


async def run_plan_expiry_loop(plan_service: DuePlanExpirer, *, interval_seconds: float = 60.0) -> None:
    """Keep unapproved plans from remaining actionable beyond their TTL."""
    if interval_seconds < 1:
        raise ValueError("plan expiry interval must be at least one second")
    last_pruned_at: datetime | None = None
    while True:
        now = datetime.now(UTC)
        reconcile = getattr(plan_service, "reconcile_exhausted_operations", None)
        if callable(reconcile):
            try:
                reconciled = await cast(Any, reconcile)()
            except Exception as exc:
                logger.warning("Plan operation reconciliation failed (non-fatal): %s", type(exc).__name__)
            else:
                if reconciled:
                    logger.warning("Reconciled %d applying plans after exhausted operations.", len(reconciled))
        try:
            expired = await plan_service.expire_due_plans()
        except Exception as exc:
            logger.warning("Plan expiry sweep failed (non-fatal): %s", type(exc).__name__)
        else:
            if expired:
                logger.info("Plan expiry sweep marked %d plans stale.", len(expired))
        if last_pruned_at is None or now - last_pruned_at >= timedelta(days=1):
            try:
                pruned = await plan_service.prune_retained_history(
                    cutoff=now - timedelta(days=PLAN_HISTORY_RETENTION_DAYS)
                )
            except Exception as exc:
                logger.warning("Automation history retention failed (non-fatal): %s", type(exc).__name__)
            else:
                if any(pruned.values()):
                    logger.info("Automation history retention pruned %s.", pruned)
                last_pruned_at = now
        await asyncio.sleep(interval_seconds)
