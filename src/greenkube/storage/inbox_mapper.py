"""Row mapping shared by the SQLite and PostgreSQL inbox repositories (migration 0070)."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from pydantic import ValidationError

from greenkube.signals.connectors.inbox import InboxBatch, InboxSignal
from greenkube.signals.model import SourceSignal
from greenkube.signals.sdk import Diagnostic
from greenkube.storage.signal_mapper import parse_json, required

logger = logging.getLogger(__name__)


def like_prefix(prefix: str) -> str:
    """A LIKE pattern matching ``prefix`` literally (``ESCAPE '\\'``)."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def inbox_signal_from_row(row: Any) -> Optional[InboxSignal]:
    data = dict(row)
    try:
        signal = SourceSignal.model_validate(parse_json(data.get("signal")))
    except ValidationError as exc:
        logger.warning("Skipping unreadable pushed signal %s: %s", data.get("signal_id"), exc.error_count())
        return None
    return InboxSignal(signal=signal, batch_id=str(data["batch_id"]), received_at=required(data["received_at"]))


def batch_from_row(row: Any) -> InboxBatch:
    data = dict(row)
    diagnostics: List[Diagnostic] = []
    for item in parse_json(data.get("diagnostics")) or []:
        try:
            diagnostics.append(Diagnostic.model_validate(item))
        except ValidationError:
            continue
    return InboxBatch(
        batch_id=str(data["batch_id"]),
        source_id=str(data["source_id"]),
        received_at=required(data["received_at"]),
        complete=bool(data.get("complete")),
        signal_count=int(data.get("signal_count") or 0),
        pushed_by=data.get("pushed_by"),
        diagnostics=diagnostics,
        withdrawn=int(data.get("withdrawn_count") or 0),
    )
