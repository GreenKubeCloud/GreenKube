"""CLI entry point for processing the durable automation queue."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

import typer

from greenkube.automation.git.base import GitProviderTransientError
from greenkube.automation.operations import digest, operation_fingerprint
from greenkube.automation.service import AutomationService
from greenkube.core.config import get_config
from greenkube.core.db import get_db_manager
from greenkube.core.factory import get_pull_request_repository, get_recommendation_repository
from greenkube.models.metrics import ApplyPrRequest

logger = logging.getLogger(__name__)
app = typer.Typer(name="automation-worker", help="Process queued GreenKube automation operations.")
MAX_ATTEMPTS = 5


def _repository():
    if get_config().DB_TYPE == "postgres":
        from greenkube.storage.postgres.automation_operation_repository import PostgresAutomationOperationRepository

        return PostgresAutomationOperationRepository(get_db_manager())
    from greenkube.storage.sqlite.automation_operation_repository import SQLiteAutomationOperationRepository

    return SQLiteAutomationOperationRepository(get_db_manager())


async def process_once(worker_id: str | None = None) -> bool:
    operations = _repository()
    await operations.reconcile()
    operation = await operations.claim_next(worker_id or str(uuid.uuid4()))
    if operation is None:
        return False
    if operation.id is None:
        raise RuntimeError("Claimed automation operation has no database ID.")
    service = AutomationService(get_recommendation_repository(), get_pull_request_repository())
    try:
        recommendation = await service.reco_repo.get_recommendation_by_id(operation.recommendation_id)
        if recommendation is None or operation.fingerprint != operation_fingerprint(recommendation, operation.request):
            raise RuntimeError("Recommendation fingerprint changed after the operation was queued.")
        request = ApplyPrRequest.model_validate(operation.request)
        result = await service.apply_recommendation_pr(operation.recommendation_id, request, actor=operation.actor)
        if result.status == "error":
            raise RuntimeError(result.message or "Automation operation failed.")
        preview_digest = digest({"diff": result.diff or "", "patch": result.patch or {}})
        commit_digest = digest({"diff": result.diff or "", "result": result.model_dump(mode="json")})
        await operations.mark_succeeded(
            operation.id,
            result=result.model_dump(mode="json"),
            preview_digest=preview_digest,
            commit_digest=commit_digest,
        )
    except GitProviderTransientError as exc:
        logger.exception("Automation operation %s failed", operation.id)
        if operation.attempts < MAX_ATTEMPTS:
            delay = min(300, 2**operation.attempts)
            from greenkube.automation.operations import utcnow

            await operations.reschedule(operation.id, str(exc), utcnow() + timedelta(seconds=delay))
        else:
            await operations.mark_failed(operation.id, str(exc))
    except Exception as exc:
        logger.exception("Automation operation %s failed", operation.id)
        await operations.mark_failed(operation.id, str(exc))
    return True


async def run_worker(poll_seconds: float = 5.0, once: bool = False) -> None:
    while True:
        processed = await process_once()
        if once or not processed:
            if once:
                return
            await asyncio.sleep(poll_seconds)


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="Process at most one operation."),
    poll_seconds: float = typer.Option(5.0, min=0.1, help="Delay when the queue is empty."),
):
    asyncio.run(run_worker(poll_seconds=poll_seconds, once=once))


if __name__ == "__main__":
    app()
