"""Push endpoints: signals from external tools (roadmap G2, G6).

Endpoints (API key scope ``signals:write``):
  POST /signals                - A batch of ``signal.v1alpha1`` signals (``source_id: push:<client>``)
  POST /sources/krr/reports    - A Robusta KRR JSON report (``krr simple -f json``)

Pushed signals are stored in the inbox and used by the next scans through the
``push`` and ``krr`` sources: they reach recommendations only through
consensus, never directly. Pushing the same signal again is idempotent: its
``signal_id`` is derived from its content and the stored row is refreshed.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from greenkube.signals.connectors.inbox import (
    MAX_INBOX_SIGNALS_PER_SOURCE,
    InboxBatch,
    SignalInboxRepository,
    create_signal_inbox_repository,
)
from greenkube.signals.connectors.krr import KRR_SOURCE_ID, KrrReportError, parse_krr_report
from greenkube.signals.connectors.push import PointerError, parse_batch
from greenkube.signals.sdk import Diagnostic

logger = logging.getLogger(__name__)

router = APIRouter()

#: Largest accepted bodies (bytes).
MAX_SIGNAL_BATCH_BYTES = 8 * 1024 * 1024
MAX_KRR_REPORT_BYTES = 64 * 1024 * 1024
#: Inbox rows not pushed again for this long are pruned on the next push.
INBOX_RETENTION = timedelta(days=30)


class PushResponse(BaseModel):
    batch_id: str
    source_id: str
    complete: bool
    received_at: datetime
    #: Signals stored or refreshed by this push.
    accepted: int
    #: Stable ids of the accepted signals (the same signal always gets the same id).
    signal_ids: List[str] = Field(default_factory=list)
    #: Signals of earlier pushes withdrawn by this complete batch.
    withdrawn: int = 0
    diagnostics: List[Diagnostic] = Field(default_factory=list)


class SignalValidationResponse(BaseModel):
    detail: str
    errors: List[PointerError]


async def get_signal_inbox() -> SignalInboxRepository:
    return create_signal_inbox_repository()


def _cluster_uid() -> str:
    from greenkube.core.config import get_config
    from greenkube.core.optimization.registry import cluster_uid

    return cluster_uid(get_config())


def _actor(request: Request) -> Optional[str]:
    principal = getattr(request.state, "principal", None)
    return getattr(principal, "actor", None)


async def _json_body(request: Request, limit: int) -> Any:
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > limit:
        raise HTTPException(status_code=413, detail=f"Body larger than {limit} bytes.")
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(status_code=413, detail=f"Body larger than {limit} bytes.")
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Body is not valid JSON.") from exc


def _invalid(errors: List[PointerError], detail: str) -> JSONResponse:
    content = SignalValidationResponse(detail=detail, errors=errors[:200]).model_dump(mode="json")
    return JSONResponse(status_code=422, content=content)


async def _store(
    inbox: SignalInboxRepository,
    *,
    source_id: str,
    complete: bool,
    signals: list,
    received_at: datetime,
    actor: Optional[str],
    diagnostics: List[Diagnostic],
) -> PushResponse:
    try:
        if not complete:
            held = await inbox.count(source_id)
            if held + len(signals) > MAX_INBOX_SIGNALS_PER_SOURCE:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{source_id}' would hold more than {MAX_INBOX_SIGNALS_PER_SOURCE} signals; "
                        "send a complete batch to replace them."
                    ),
                )
        batch = await inbox.accept(
            InboxBatch(
                batch_id=uuid.uuid4().hex,
                source_id=source_id,
                received_at=received_at,
                complete=complete,
                signal_count=len(signals),
                pushed_by=actor,
                diagnostics=diagnostics,
            ),
            signals,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Could not store pushed signals for '%s' (%s).", source_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="The signal inbox is unavailable.") from exc
    try:
        await inbox.prune(received_at - INBOX_RETENTION)
    except Exception as exc:  # retention is best effort
        logger.debug("Could not prune the signal inbox (%s).", type(exc).__name__)
    return PushResponse(
        batch_id=batch.batch_id,
        source_id=source_id,
        complete=complete,
        received_at=received_at,
        accepted=len(signals),
        signal_ids=[signal.signal_id for signal in signals],
        withdrawn=batch.withdrawn,
        diagnostics=diagnostics,
    )


@router.post(
    "/signals",
    response_model=PushResponse,
    responses={
        400: {"description": "The body is not JSON."},
        409: {"description": "Too many signals held for this client; send a complete batch."},
        413: {"description": "Body too large."},
        422: {"model": SignalValidationResponse, "description": "Schema errors, located by JSON pointers."},
    },
)
async def push_signals(request: Request, inbox: SignalInboxRepository = Depends(get_signal_inbox)):
    """Push a batch of signals (schema ``signal-batch.v1alpha1``); idempotent on ``signal_id``.

    ``complete: true`` replaces every signal the client pushed before;
    ``complete: false`` adds or refreshes signals. The next scans replay them
    through the ``push`` source; they become recommendations only through consensus.
    """
    document = await _json_body(request, MAX_SIGNAL_BATCH_BYTES)
    received_at = datetime.now(timezone.utc)
    batch, signals, errors = parse_batch(document, cluster_uid=_cluster_uid(), received_at=received_at)
    if batch is None:
        return _invalid(errors, "The signal batch does not match schema signal-batch.v1alpha1.")
    unique = list({signal.signal_id: signal for signal in signals}.values())
    diagnostics: List[Diagnostic] = []
    if len(unique) < len(signals):
        diagnostics.append(
            Diagnostic(
                code="duplicate_signal", message="identical signals were merged", count=len(signals) - len(unique)
            )
        )
    return await _store(
        inbox,
        source_id=batch.source_id,
        complete=batch.complete,
        signals=unique,
        received_at=received_at,
        actor=_actor(request),
        diagnostics=diagnostics,
    )


@router.post(
    "/sources/krr/reports",
    response_model=PushResponse,
    responses={
        400: {"description": "The body is not JSON."},
        413: {"description": "Body too large."},
        422: {"model": SignalValidationResponse, "description": "The body is not a KRR JSON report."},
    },
)
async def push_krr_report(
    request: Request,
    complete: bool = Query(
        True,
        description="True when the report covers every workload KRR sizes (replaces earlier reports); "
        "false for a partial report (for example one namespace), which only adds or refreshes advice.",
    ),
    cluster: Optional[str] = Query(
        None,
        max_length=253,
        description="The kubeconfig context KRR scanned this GreenKube's cluster in. Required when the report "
        "covers several clusters (krr --all-clusters or several -c); entries of other clusters are skipped.",
    ),
    inbox: SignalInboxRepository = Depends(get_signal_inbox),
):
    """Push a Robusta KRR report (``krr simple -f json``); its signals are used by the next scans."""
    document = await _json_body(request, MAX_KRR_REPORT_BYTES)
    received_at = datetime.now(timezone.utc)
    try:
        parsed = parse_krr_report(
            document, cluster_uid=_cluster_uid(), received_at=received_at, cluster_context=cluster
        )
    except KrrReportError as exc:
        detail = "Not a KRR JSON report." if exc.type == "krr_report" else "The KRR report cannot be attributed."
        return _invalid([PointerError(pointer=exc.pointer, message=str(exc), type=exc.type)], detail)
    return await _store(
        inbox,
        source_id=KRR_SOURCE_ID,
        complete=complete,
        signals=parsed.signals,
        received_at=received_at,
        actor=_actor(request),
        diagnostics=parsed.diagnostics,
    )
