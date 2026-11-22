"""Robusta KRR connector (roadmap G2).

KRR (``krr simple -f json``) writes one JSON report per run. Users push it to
``POST /api/v1/sources/krr/reports`` (from CI or a CronJob of their own, see
``docs/sources/krr.md``); the report is parsed into signals at once and stored
in the inbox, and the ``krr`` source reads them into each scan.

Mapping, pinned against the report shape of KRR 1.x (``Result`` →
``scans[]`` of ``ResourceScan``; ``tests/sources/fixtures/krr_report.json``):

* ``object`` (cluster, namespace, kind, name, container) → :class:`TargetRef`
  in this GreenKube's cluster (KRR's ``cluster`` is a kubeconfig context name;
  it is kept in ``raw``). A report that names several clusters
  (``--all-clusters``, several ``-c``) is refused unless ``cluster_context``
  selects one: another cluster's advice is never attributed to same-named
  workloads here;
* ``recommended.requests``/``limits`` per resource → one
  ``ContainerResourcesProposal`` per resource (``RIGHTSIZING_CPU``,
  ``RIGHTSIZING_MEMORY``, the kinds of every container-request source),
  decreases *and* increases. CPU is in cores, memory in bytes; ``"?"``
  (not enough data) skips the resource, ``null`` means "no value" (KRR's
  ``simple`` strategy recommends no CPU limit);
* ``strategy.name``/``settings`` → ``algorithm`` (``krr.simple``, params such as
  ``cpu_percentile``, ``memory_buffer_percentage``, ``history_duration``; the
  window is the history duration before the report was received);
* the per-resource ``severity`` → a confidence hint (see :data:`SEVERITY_CONFIDENCE`)
  and ``evidence.data.severity``.

KRR is in maintenance mode: unknown fields are ignored, an entry that cannot
be read is skipped with a diagnostic, never failing the whole report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Dict, FrozenSet, List, Mapping, Optional, Tuple

from greenkube.signals.connectors.inbox import InboxSource
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    ResourceSpec,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
    TimeWindow,
    trim_raw,
)
from greenkube.signals.sdk import Diagnostic, SourceTier

KRR_SOURCE_ID = "krr"

#: KRR severity (how far the current request is from the recommendation) → confidence hint.
#: KRR reports no confidence of its own; the hint only orders KRR's advice for consensus.
SEVERITY_CONFIDENCE: Dict[str, Optional[float]] = {
    "CRITICAL": 0.8,
    "WARNING": 0.7,
    "OK": 0.6,
    "GOOD": 0.6,
    "UNKNOWN": None,
}

_KIND_BY_RESOURCE = {"cpu": "RIGHTSIZING_CPU", "memory": "RIGHTSIZING_MEMORY"}
_MIB = 1024 * 1024


class KrrReportError(ValueError):
    """The document is not a usable KRR JSON report (422, located by a JSON pointer)."""

    def __init__(self, message: str, *, pointer: str = "", type: str = "krr_report") -> None:
        super().__init__(message)
        self.pointer = pointer
        self.type = type


@dataclass
class KrrParseResult:
    signals: List[SourceSignal] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    #: Scan entries read (those of the selected cluster).
    entries: int = 0
    strategy: Optional[str] = None


def _number(value: Any) -> Tuple[Optional[float], str]:
    """``(value, state)``: state is ``ok``, ``unset`` (null) or ``unknown`` (``"?"`` or unreadable)."""
    if isinstance(value, Mapping):
        value = value.get("value")
    if value is None:
        return None, "unset"
    if isinstance(value, bool):
        return None, "unknown"
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None, "unknown"
    else:
        return None, "unknown"
    if math.isnan(number) or math.isinf(number) or number < 0:
        return None, "unknown"
    return number, "ok"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _severity(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("severity")
    return str(value).upper() if isinstance(value, str) and value else None


def _scaled(resource: str, value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return int(round(value * 1000)) if resource == "cpu" else int(round(value))


def _fmt(resource: str, value: Optional[int]) -> str:
    if value is None:
        return "unset"
    return f"{value}m" if resource == "cpu" else f"{value / _MIB:.0f}Mi"


def _history_hours(settings: Mapping[str, Any]) -> Optional[float]:
    value, state = _number(settings.get("history_duration"))
    return value if state == "ok" and value and value > 0 else None


def _entry_raw(entry: Mapping[str, Any]) -> Dict[str, Any]:
    obj = dict(entry.get("object") or {})
    pods = obj.pop("pods", None)
    obj.pop("labels", None)
    obj.pop("annotations", None)
    if isinstance(pods, list):
        obj["pod_count"] = len(pods)
    return trim_raw({"object": obj, "recommended": entry.get("recommended"), "severity": entry.get("severity")})


def _entry_cluster(entry: Any) -> Optional[str]:
    """The kubeconfig context KRR scanned an entry in (``None`` when unnamed, e.g. in-cluster)."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("object"), Mapping):
        return None
    value = entry["object"].get("cluster")
    return value if isinstance(value, str) and value else None


def _select_cluster(scans: List[Any], cluster_context: Optional[str]) -> Tuple[List[Any], int]:
    """Scan entries of one cluster and the count of entries of other clusters.

    KRR's ``cluster`` is a kubeconfig context name, which GreenKube cannot map
    to its own cluster: a report naming several clusters is refused rather
    than guessed, unless ``cluster_context`` names the one to keep.
    """
    named = sorted({name for name in map(_entry_cluster, scans) if name})
    if cluster_context:
        if not named:
            # An unnamed (in-cluster) report is about one cluster: nothing to select.
            return scans, 0
        if cluster_context not in named:
            raise KrrReportError(
                f"the report has no scan of cluster {cluster_context!r} (it covers {', '.join(named)})",
                pointer="/scans",
                type="cluster_not_in_report",
            )
        kept = [entry for entry in scans if _entry_cluster(entry) in (cluster_context, None)]
        return kept, len(scans) - len(kept)
    if len(named) > 1:
        raise KrrReportError(
            f"the report covers several clusters ({', '.join(named)}); push one cluster's report "
            "or select the one GreenKube runs in with ?cluster=<context>",
            pointer="/scans",
            type="multiple_clusters",
        )
    return scans, 0


def parse_krr_report(
    document: Any, *, cluster_uid: str, received_at: datetime, cluster_context: Optional[str] = None
) -> KrrParseResult:
    """Signals of a KRR JSON report; raises :class:`KrrReportError` when it is not one.

    ``cluster_context`` keeps the entries of that kubeconfig context only; it
    is required when the report names several clusters.
    """
    if not isinstance(document, Mapping):
        raise KrrReportError("a KRR report is a JSON object with a 'scans' array (krr simple -f json)")
    scans = document.get("scans")
    if not isinstance(scans, list):
        raise KrrReportError("the report has no 'scans' array; run krr with '-f json'")
    scans, other_cluster = _select_cluster(scans, cluster_context)
    strategy = _mapping(document.get("strategy"))
    strategy_name = str(strategy.get("name") or "simple")
    params = dict(_mapping(strategy.get("settings")))
    history = _history_hours(params)
    window = TimeWindow(start=received_at - timedelta(hours=history), end=received_at) if history else None
    algorithm = AlgorithmInfo(name=f"krr.{strategy_name}", params=trim_raw(params), window=window)

    result = KrrParseResult(entries=len(scans), strategy=strategy_name)
    skipped: Dict[str, int] = {}
    if other_cluster:
        skipped["other_cluster"] = other_cluster

    def skip(code: str) -> None:
        skipped[code] = skipped.get(code, 0) + 1

    seen: set[str] = set()
    for entry in scans:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("object"), Mapping):
            skip("invalid_entry")
            continue
        obj = entry["object"]
        namespace, kind, name, container = (obj.get(key) for key in ("namespace", "kind", "name", "container"))
        if not all(isinstance(value, str) and value for value in (namespace, kind, name, container)):
            skip("invalid_entry")
            continue
        recommended = _mapping(entry.get("recommended"))
        rec_requests = _mapping(recommended.get("requests"))
        rec_limits = _mapping(recommended.get("limits"))
        info = _mapping(recommended.get("info"))
        allocations = _mapping(obj.get("allocations"))
        cur_requests = _mapping(allocations.get("requests"))
        cur_limits = _mapping(allocations.get("limits"))
        try:
            target = TargetRef.for_kind(cluster_uid, kind, name, namespace=namespace, container=container)
        except ValueError:
            skip("invalid_entry")
            continue
        for resource, rec_kind in _KIND_BY_RESOURCE.items():
            proposed_value, state = _number(rec_requests.get(resource))
            if state == "unknown":
                skip("not_enough_data")
                continue
            if proposed_value is None:
                skip("no_recommendation")
                continue
            proposed = _scaled(resource, proposed_value)
            current_value, current_state = _number(cur_requests.get(resource))
            current = _scaled(resource, current_value) if current_state == "ok" else None
            if current is not None and current == proposed:
                skip("unchanged")
                continue
            limit_value, limit_state = _number(rec_limits.get(resource))
            proposed_limit = _scaled(resource, limit_value) if limit_state == "ok" else None
            cur_limit_value, cur_limit_state = _number(cur_limits.get(resource))
            current_limit = _scaled(resource, cur_limit_value) if cur_limit_state == "ok" else None
            if resource == "cpu":
                current_spec = ResourceSpec(cpu_request_m=current, cpu_limit_m=current_limit)
                proposed_spec = ResourceSpec(cpu_request_m=proposed, cpu_limit_m=proposed_limit)
            else:
                current_spec = ResourceSpec(memory_request_bytes=current, memory_limit_bytes=current_limit)
                proposed_spec = ResourceSpec(memory_request_bytes=proposed, memory_limit_bytes=proposed_limit)
            proposal = ContainerResourcesProposal(container=container, current=current_spec, proposed=proposed_spec)
            severity = _severity(rec_requests.get(resource)) or _severity(entry.get("severity"))
            verb = {"decrease": "reduce", "increase": "raise"}.get(proposal.direction, "set")
            label = f"{kind} {namespace}/{name} container '{container}'"
            note = info.get(resource) if isinstance(info.get(resource), str) else None
            data: Dict[str, Any] = {"severity": severity, "limit_state": limit_state}
            if limit_state == "unset":
                data["limit_advice"] = f"KRR recommends no {resource} limit"
            signal = SourceSignal(
                source_id=KRR_SOURCE_ID,
                source_ref=f"{namespace}/{kind}/{name}",
                target=target,
                capability=Capability.CONTAINER_REQUESTS,
                kind=rec_kind,
                proposal=proposal,
                algorithm=algorithm,
                evidence=SignalEvidence(
                    summary=(
                        f"KRR ({strategy_name}) recommends to {verb} the {resource} request of {label} "
                        f"from {_fmt(resource, current)} to {_fmt(resource, proposed)}."
                    ),
                    details=note or "",
                    data=data,
                ),
                observed_window=window,
                confidence=SEVERITY_CONFIDENCE.get(severity or "UNKNOWN"),
                provenance=SignalProvenance(origin=SignalOrigin.PUSH),
                raw=_entry_raw(entry),
                fetched_at=received_at,
            )
            if signal.signal_id in seen:
                skip("duplicate")
                continue
            seen.add(signal.signal_id)
            result.signals.append(signal)
    messages = {
        "invalid_entry": "scan entries without a readable object (namespace, kind, name, container) were skipped",
        "not_enough_data": "resources KRR could not size ('?': not enough data) were skipped",
        "no_recommendation": "resources without a recommended request were skipped",
        "unchanged": "resources whose recommendation equals the current request were skipped",
        "duplicate": "duplicate scan entries were merged",
        "other_cluster": f"scan entries of other clusters than {cluster_context!r} were skipped",
    }
    for code, count in sorted(skipped.items()):
        level = "warning" if code in ("invalid_entry", "other_cluster") else "info"
        result.diagnostics.append(Diagnostic(level=level, code=code, message=messages[code], count=count))
    errors = document.get("errors")
    if isinstance(errors, list) and errors:
        result.diagnostics.append(
            Diagnostic(level="warning", code="krr_errors", message="KRR reported errors in its run", count=len(errors))
        )
    return result


class KrrSource(InboxSource):
    """Robusta KRR recommendations pushed as JSON reports (roadmap G2)."""

    id: ClassVar[str] = KRR_SOURCE_ID
    display_name: ClassVar[str] = "Robusta KRR"
    tier: ClassVar[SourceTier] = SourceTier.OSS
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    description: ClassVar[str] = (
        "Container request recommendations from KRR JSON reports pushed to POST /api/v1/sources/krr/reports "
        "(from CI or a CronJob, see docs/sources/krr.md)."
    )
    inbox_source_id: ClassVar[Optional[str]] = KRR_SOURCE_ID
    push_hint: ClassVar[str] = (
        "POST 'krr simple -f json' output to /api/v1/sources/krr/reports with a signals:report API key "
        "(docs/sources/krr.md)"
    )
