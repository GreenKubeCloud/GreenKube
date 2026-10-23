# src/greenkube/storage/recommendation_mapper.py
"""Shared mapping between recommendation_history rows and RecommendationRecord.

Both the SQLite and PostgreSQL repositories use this mapper so that schema
changes only need to be reflected in one place.
"""

import json
from datetime import datetime, timezone
from typing import Any, List, Optional

from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import (
    EffortLevel,
    RecommendationCapability,
    RecommendationRecord,
    RecommendationSource,
    RecommendationStatus,
    RecommendationType,
    RiskLevel,
)


def to_json(value: Any) -> Optional[str]:
    """Serializes a JSON-compatible value for storage, or None."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value)


def from_json(value: Any, default: Any = None) -> Any:
    """Deserializes a stored JSON value, tolerating NULL and already-decoded values."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def evidence_to_json(evidence: Optional[RecommendationEvidence]) -> Optional[str]:
    """Serializes a recommendation evidence block for storage."""
    if evidence is None:
        return None
    return evidence.model_dump_json()


def evidence_from_json(value: Any) -> Optional[RecommendationEvidence]:
    """Deserializes a stored evidence block, tolerating malformed legacy rows."""
    if value is None:
        return None
    if isinstance(value, RecommendationEvidence):
        return value
    try:
        return RecommendationEvidence.model_validate_json(value)
    except Exception:
        return None


def sources_to_json(sources: Optional[List[str]]) -> str:
    """Serializes the provenance list for storage."""
    return json.dumps(list(sources or []))


def sources_from_json(value: Any) -> List[str]:
    """Deserializes a stored provenance list, tolerating NULL and legacy rows."""
    parsed = from_json(value, default=[])
    if isinstance(parsed, (list, tuple)):
        return [str(item) for item in parsed]
    return []


def _as_source(value: Any) -> RecommendationSource:
    try:
        return RecommendationSource(value)
    except ValueError:
        return RecommendationSource.GREENKUBE


def _as_capability(value: Any) -> Optional[RecommendationCapability]:
    if value is None:
        return None
    try:
        return RecommendationCapability(value)
    except ValueError:
        return None


def _as_risk_level(value: Any) -> Optional[RiskLevel]:
    if value is None:
        return None
    try:
        return RiskLevel(value)
    except ValueError:
        return None


def _as_effort(value: Any) -> Optional[EffortLevel]:
    if value is None:
        return None
    try:
        return EffortLevel(value)
    except ValueError:
        return None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "off", ""}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    return bool(value)


def _as_datetime(value: Any) -> Optional[datetime]:
    """Normalizes a database timestamp (native datetime or ISO string) to datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Returns a column value, falling back to ``default`` when NULL or absent."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


#: Canonical column order for an append-only insert.
RECORD_COLUMNS: tuple = (
    "pod_name",
    "container_name",
    "namespace",
    "type",
    "description",
    "reason",
    "priority",
    "scope",
    "status",
    "potential_savings_cost",
    "potential_savings_co2e_grams",
    "current_cpu_request_millicores",
    "recommended_cpu_request_millicores",
    "current_memory_request_bytes",
    "recommended_memory_request_bytes",
    "cron_schedule",
    "target_node",
    "identity_version",
    "fingerprint",
    "source",
    "source_ref",
    "sources",
    "superseded_by",
    "capability",
    "owner_kind",
    "owner_name",
    "evidence",
    "risk_level",
    "risk_factors",
    "confidence",
    "effort",
    "ranking_score",
    "ranking_factors",
    "patch",
    "expires_at",
    "reversible",
    "requires_restart",
    "applied_at",
    "actual_cpu_request_millicores",
    "actual_memory_request_bytes",
    "carbon_saved_co2e_grams",
    "cost_saved",
    "ignored_at",
    "ignored_reason",
    "application_method",
    "verified_at",
    "verification_status",
    "verification_window_start",
    "verification_window_end",
    "baseline",
    "measured_co2e_saved_grams",
    "measured_cost_saved",
    "savings_realized",
    "created_at",
    "updated_at",
)

#: Columns refreshed in place when an active recommendation is upserted.
ACTIVE_UPSERT_COLUMNS: tuple = (
    "description",
    "reason",
    "priority",
    "scope",
    "container_name",
    "potential_savings_cost",
    "potential_savings_co2e_grams",
    "current_cpu_request_millicores",
    "recommended_cpu_request_millicores",
    "current_memory_request_bytes",
    "recommended_memory_request_bytes",
    "cron_schedule",
    "target_node",
    "identity_version",
    "fingerprint",
    "source",
    "source_ref",
    "sources",
    "superseded_by",
    "capability",
    "owner_kind",
    "owner_name",
    "evidence",
    "risk_level",
    "risk_factors",
    "confidence",
    "effort",
    "ranking_score",
    "ranking_factors",
    "patch",
    "expires_at",
    "reversible",
    "requires_restart",
)


def record_values(record: RecommendationRecord, encode_datetime=lambda value: value) -> dict:
    """Returns a ``{column: bind_value}`` mapping for a recommendation record.

    Args:
        record: The record to serialize.
        encode_datetime: Callable applied to non-null datetime values
            (SQLite stores ISO strings; PostgreSQL binds native datetimes).
    """
    enc = encode_datetime

    def _dt(value):
        return enc(value) if value is not None else None

    def _enum(value):
        return value.value if hasattr(value, "value") else value

    return {
        "pod_name": record.pod_name,
        "container_name": record.container_name,
        "namespace": record.namespace,
        "type": _enum(record.type),
        "description": record.description,
        "reason": record.reason,
        "priority": record.priority,
        "scope": record.scope,
        "status": _enum(record.status),
        "potential_savings_cost": record.potential_savings_cost,
        "potential_savings_co2e_grams": record.potential_savings_co2e_grams,
        "current_cpu_request_millicores": record.current_cpu_request_millicores,
        "recommended_cpu_request_millicores": record.recommended_cpu_request_millicores,
        "current_memory_request_bytes": record.current_memory_request_bytes,
        "recommended_memory_request_bytes": record.recommended_memory_request_bytes,
        "cron_schedule": record.cron_schedule,
        "target_node": record.target_node,
        "identity_version": record.identity_version,
        "fingerprint": record.fingerprint,
        "source": _enum(record.source),
        "source_ref": record.source_ref,
        "sources": sources_to_json(record.sources),
        "superseded_by": record.superseded_by,
        "capability": _enum(record.capability),
        "owner_kind": record.owner_kind,
        "owner_name": record.owner_name,
        "evidence": evidence_to_json(record.evidence),
        "risk_level": _enum(record.risk_level),
        "risk_factors": to_json(list(record.risk_factors)),
        "confidence": record.confidence,
        "effort": _enum(record.effort),
        "ranking_score": record.ranking_score,
        "ranking_factors": to_json(dict(record.ranking_factors)),
        "patch": to_json(record.patch),
        "expires_at": _dt(record.expires_at),
        "reversible": record.reversible,
        "requires_restart": record.requires_restart,
        "applied_at": _dt(record.applied_at),
        "actual_cpu_request_millicores": record.actual_cpu_request_millicores,
        "actual_memory_request_bytes": record.actual_memory_request_bytes,
        "carbon_saved_co2e_grams": record.carbon_saved_co2e_grams,
        "cost_saved": record.cost_saved,
        "ignored_at": _dt(record.ignored_at),
        "ignored_reason": record.ignored_reason,
        "application_method": record.application_method,
        "verified_at": _dt(record.verified_at),
        "verification_status": record.verification_status,
        "verification_window_start": _dt(record.verification_window_start),
        "verification_window_end": _dt(record.verification_window_end),
        "baseline": to_json(record.baseline),
        "measured_co2e_saved_grams": record.measured_co2e_saved_grams,
        "measured_cost_saved": record.measured_cost_saved,
        "savings_realized": record.savings_realized,
        "created_at": _dt(record.created_at),
        "updated_at": _dt(record.updated_at),
    }


def row_to_record(row: Any) -> RecommendationRecord:
    """Converts a database row to a RecommendationRecord.

    Supports both ``aiosqlite.Row`` and ``asyncpg.Record``: both expose
    mapping-style access by column name.
    """
    return RecommendationRecord(
        id=row["id"],
        pod_name=row["pod_name"],
        container_name=_get(row, "container_name"),
        namespace=row["namespace"],
        type=RecommendationType(row["type"]),
        description=row["description"],
        reason=_get(row, "reason", "") or "",
        priority=_get(row, "priority", "medium") or "medium",
        scope=_get(row, "scope", "pod") or "pod",
        status=RecommendationStatus(_get(row, "status", "active") or "active"),
        potential_savings_cost=_get(row, "potential_savings_cost"),
        potential_savings_co2e_grams=_get(row, "potential_savings_co2e_grams"),
        current_cpu_request_millicores=_get(row, "current_cpu_request_millicores"),
        recommended_cpu_request_millicores=_get(row, "recommended_cpu_request_millicores"),
        current_memory_request_bytes=_get(row, "current_memory_request_bytes"),
        recommended_memory_request_bytes=_get(row, "recommended_memory_request_bytes"),
        cron_schedule=_get(row, "cron_schedule"),
        target_node=_get(row, "target_node"),
        identity_version=int(_get(row, "identity_version", 2) or 2),
        fingerprint=_get(row, "fingerprint"),
        source=_as_source(_get(row, "source", "greenkube")),
        source_ref=_get(row, "source_ref"),
        sources=sources_from_json(_get(row, "sources")),
        superseded_by=_get(row, "superseded_by"),
        capability=_as_capability(_get(row, "capability")),
        owner_kind=_get(row, "owner_kind"),
        owner_name=_get(row, "owner_name"),
        evidence=evidence_from_json(_get(row, "evidence")),
        risk_level=_as_risk_level(_get(row, "risk_level")),
        risk_factors=sources_from_json(_get(row, "risk_factors")),
        confidence=_get(row, "confidence"),
        effort=_as_effort(_get(row, "effort")),
        ranking_score=_get(row, "ranking_score"),
        ranking_factors=from_json(_get(row, "ranking_factors"), default={}),
        patch=from_json(_get(row, "patch")),
        expires_at=_as_datetime(_get(row, "expires_at")),
        reversible=_as_bool(_get(row, "reversible")),
        requires_restart=_as_bool(_get(row, "requires_restart")),
        applied_at=_as_datetime(_get(row, "applied_at")),
        actual_cpu_request_millicores=_get(row, "actual_cpu_request_millicores"),
        actual_memory_request_bytes=_get(row, "actual_memory_request_bytes"),
        carbon_saved_co2e_grams=_get(row, "carbon_saved_co2e_grams"),
        cost_saved=_get(row, "cost_saved"),
        ignored_at=_as_datetime(_get(row, "ignored_at")),
        ignored_reason=_get(row, "ignored_reason"),
        application_method=_get(row, "application_method"),
        verified_at=_as_datetime(_get(row, "verified_at")),
        verification_status=_get(row, "verification_status"),
        verification_window_start=_as_datetime(_get(row, "verification_window_start")),
        verification_window_end=_as_datetime(_get(row, "verification_window_end")),
        baseline=from_json(_get(row, "baseline")),
        measured_co2e_saved_grams=_get(row, "measured_co2e_saved_grams"),
        measured_cost_saved=_get(row, "measured_cost_saved"),
        savings_realized=_as_bool(_get(row, "savings_realized")),
        created_at=_as_datetime(row["created_at"]) or datetime.now(timezone.utc),
        updated_at=_as_datetime(_get(row, "updated_at")),
    )
