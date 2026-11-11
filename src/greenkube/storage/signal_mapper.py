"""Row mapping shared by the SQLite and PostgreSQL signal repositories."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from pydantic import ValidationError

from greenkube.signals.model import SourceSignal
from greenkube.signals.runtime import SourceRunRecord, SourceRunStatus
from greenkube.signals.sdk import Diagnostic, SourceHealthStatus
from greenkube.storage.base_signal_repository import StoredSignal

logger = logging.getLogger(__name__)

RUN_COLUMNS = (
    "optimization_run_id",
    "source_id",
    "source_version",
    "namespace",
    "status",
    "complete",
    "health_status",
    "health_reason",
    "started_at",
    "finished_at",
    "duration_seconds",
    "signal_count",
    "recommendation_count",
    "consecutive_failures",
    "last_collected_at",
    "error_code",
    "error",
    "diagnostics",
)


def parse_json(value: Any) -> Any:
    if value is None or not isinstance(value, (str, bytes)):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def aware(value: Any) -> Optional[datetime]:
    """Timestamp from a datetime or an ISO string, always UTC-aware."""
    if value is None or value == "":
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def required(value: Any) -> datetime:
    """A NOT NULL timestamp column."""
    parsed = aware(value)
    if parsed is None:
        raise ValueError("missing timestamp")
    return parsed


def diagnostics_json(run: SourceRunRecord) -> str:
    return json.dumps([item.model_dump(mode="json") for item in run.diagnostics], separators=(",", ":"))


def signal_json(signal: SourceSignal) -> str:
    return signal.model_dump_json()


def run_from_row(row: Any) -> SourceRunRecord:
    data = dict(row)
    diagnostics: List[Diagnostic] = []
    for item in parse_json(data.get("diagnostics")) or []:
        try:
            diagnostics.append(Diagnostic.model_validate(item))
        except ValidationError:
            continue
    health = data.get("health_status")
    return SourceRunRecord(
        id=data.get("id"),
        optimization_run_id=data.get("optimization_run_id"),
        source_id=data["source_id"],
        source_version=data.get("source_version"),
        namespace=data.get("namespace"),
        status=SourceRunStatus(data["status"]),
        complete=bool(data.get("complete")),
        health_status=SourceHealthStatus(health) if health in SourceHealthStatus._value2member_map_ else None,
        health_reason=data.get("health_reason"),
        started_at=required(data["started_at"]),
        finished_at=required(data["finished_at"]),
        duration_seconds=float(data.get("duration_seconds") or 0.0),
        signal_count=int(data.get("signal_count") or 0),
        recommendation_count=int(data.get("recommendation_count") or 0),
        consecutive_failures=int(data.get("consecutive_failures") or 0),
        last_collected_at=aware(data.get("last_collected_at")),
        error_code=data.get("error_code"),
        error=data.get("error"),
        diagnostics=diagnostics,
    )


def stored_signal_from_row(row: Any) -> Optional[StoredSignal]:
    data = dict(row)
    payload = parse_json(data.get("signal"))
    try:
        signal = SourceSignal.model_validate(payload)
    except ValidationError as exc:
        logger.warning("Skipping unreadable stored signal %s: %s", data.get("signal_id"), exc.error_count())
        return None
    return StoredSignal(
        signal=signal,
        first_seen_at=required(data["first_seen_at"]),
        last_seen_at=required(data["last_seen_at"]),
        last_run_id=data.get("last_run_id"),
        recommendation_fingerprint=data.get("recommendation_fingerprint"),
    )
