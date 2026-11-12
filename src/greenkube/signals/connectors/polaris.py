"""Fairwinds Polaris hygiene findings (roadmap G4).

Polaris (``polaris audit --format=json``) checks workloads against best
practices. Users push its JSON report to ``POST /api/v1/sources/polaris/reports``
(from CI, or from the optional CronJob of the Helm chart); the report is parsed
into signals at once and stored in the inbox, and the ``polaris`` source
replays them into each scan.

Mapping, pinned against the report shape of Polaris 9.x (``AuditData`` →
``Results[]`` of ``Result``, each with controller ``Results``, ``PodResult.Results``
and ``PodResult.ContainerResults[].Results``, every check a ``ResultMessage``
with ``ID``, ``Message``, ``Success``, ``Severity`` and ``Category``;
``tests/sources/fixtures/polaris_report.json``):

* only failed checks of severity ``warning`` or ``danger`` become signals
  (``ignore`` checks and successes are counted in diagnostics);
* every finding is a ``capability=hygiene`` signal with an
  :class:`~greenkube.signals.model.AdvisoryProposal`: it is shown as hygiene
  advice and never becomes a pull request by itself. Its finding kind is the
  check id in upper snake case (``cpuRequestsMissing`` → ``CPU_REQUESTS_MISSING``);
* ``cpuRequestsMissing`` / ``memoryRequestsMissing`` also get a **native
  follow-up** at scan time: when GreenKube observed the container's usage
  (at least ``RECOMMENDATION_MIN_SAMPLES`` samples) and still sees no request,
  the source proposes the request GreenKube computes, p95 × ``RIGHTSIZING_HEADROOM``
  (at least ``RECOMMENDATION_MIN_CPU_MILLICORES`` / ``RECOMMENDATION_MIN_MEMORY_BYTES``),
  as a ``container_requests`` signal (``RIGHTSIZING_CPU``/``RIGHTSIZING_MEMORY``,
  engine family ``greenkube.hygiene``). It reaches recommendations through
  consensus like any other signal; Polaris itself never sizes anything.

Polaris's own ``Score`` and ``ClusterInfo`` are kept for diagnostics only.
Unknown fields are ignored; an entry that cannot be read is skipped with a
diagnostic, never failing the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, FrozenSet, Iterator, List, Mapping, Optional, Tuple

from greenkube.signals.connectors.replay import InboxSource
from greenkube.signals.model import (
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    ResourceSpec,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
    trim_raw,
)
from greenkube.signals.sdk import CollectContext, Diagnostic, SourceBatch, SourceTier

POLARIS_SOURCE_ID = "polaris"
#: Engine family of the follow-up requests GreenKube computes for missing-request findings.
FOLLOW_UP_ENGINE = "greenkube.hygiene"
FOLLOW_UP_ALGORITHM = "greenkube.requests_from_usage"
#: Polaris checks whose finding gets a native follow-up proposal, and the resource it sizes.
MISSING_REQUEST_CHECKS: Dict[str, str] = {"cpuRequestsMissing": "cpu", "memoryRequestsMissing": "memory"}
#: Severities that become findings (``ignore`` checks are configured off in Polaris).
REPORTED_SEVERITIES = frozenset({"warning", "danger"})
#: Polaris severity → confidence hint of the finding.
SEVERITY_CONFIDENCE: Dict[str, float] = {"danger": 0.8, "warning": 0.6}
_MIB = 1024 * 1024


class PolarisReportError(ValueError):
    """The document is not a usable Polaris JSON report (422, located by a JSON pointer)."""

    def __init__(self, message: str, *, pointer: str = "", type: str = "polaris_report") -> None:
        super().__init__(message)
        self.pointer = pointer
        self.type = type


@dataclass
class PolarisParseResult:
    signals: List[SourceSignal] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    #: Controller entries read.
    entries: int = 0


def finding_kind(check_id: str) -> str:
    """``cpuRequestsMissing`` → ``CPU_REQUESTS_MISSING``."""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", check_id)
    return re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").upper() or "POLARIS_FINDING"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _checks(results: Any) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    """``(check id, result)`` of a Polaris ``ResultSet`` (an object keyed by check id)."""
    for key, result in _mapping(results).items():
        if isinstance(result, Mapping):
            yield str(result.get("ID") or key), result


def parse_polaris_report(document: Any, *, cluster_uid: str, received_at: datetime) -> PolarisParseResult:
    """Hygiene signals of a Polaris JSON report; raises :class:`PolarisReportError` when it is not one."""
    if not isinstance(document, Mapping):
        raise PolarisReportError("a Polaris report is a JSON object with a 'Results' array (polaris audit -f json)")
    entries = document.get("Results")
    if not isinstance(entries, list):
        raise PolarisReportError("the report has no 'Results' array; run 'polaris audit --format=json'", pointer="")
    version = document.get("PolarisOutputVersion")
    algorithm = AlgorithmInfo(
        name="polaris.audit",
        params={"output_version": str(version)} if isinstance(version, (str, int, float)) else {},
    )
    result = PolarisParseResult(entries=len(entries))
    counts: Dict[str, int] = {}

    def count(code: str, amount: int = 1) -> None:
        counts[code] = counts.get(code, 0) + amount

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            count("invalid_entry")
            continue
        namespace, kind, name = entry.get("Namespace"), entry.get("Kind"), entry.get("Name")
        if not all(isinstance(value, str) and value for value in (kind, name)):
            count("invalid_entry")
            continue
        namespace = namespace if isinstance(namespace, str) and namespace else None
        pod = _mapping(entry.get("PodResult"))
        scopes: List[Tuple[Optional[str], Any]] = [(None, entry.get("Results")), (None, pod.get("Results"))]
        for container in pod.get("ContainerResults") or []:
            if isinstance(container, Mapping) and isinstance(container.get("Name"), str) and container["Name"]:
                scopes.append((container["Name"], container.get("Results")))
        for container_name, results in scopes:
            for check_id, check in _checks(results):
                if check.get("Success") is not False:
                    count("passed")
                    continue
                severity = str(check.get("Severity") or "").lower()
                if severity not in REPORTED_SEVERITIES:
                    count("ignored_severity")
                    continue
                try:
                    target = TargetRef.for_kind(
                        cluster_uid, str(kind), str(name), namespace=namespace, container=container_name
                    )
                except ValueError:
                    count("invalid_entry")
                    continue
                message = str(check.get("Message") or check_id)[:500]
                category = str(check.get("Category") or "")
                label = f"{kind} {namespace + '/' if namespace else ''}{name}"
                if container_name:
                    label += f" container '{container_name}'"
                data: Dict[str, Any] = {"check": check_id, "severity": severity, "category": category}
                if check_id in MISSING_REQUEST_CHECKS:
                    data["follow_up"] = FOLLOW_UP_ALGORITHM
                signal = SourceSignal(
                    source_id=POLARIS_SOURCE_ID,
                    source_version=str(version) if isinstance(version, (str, int, float)) else None,
                    source_ref=f"{namespace or '_'}/{kind}/{name}",
                    target=target,
                    capability=Capability.HYGIENE,
                    kind=finding_kind(check_id),
                    proposal=AdvisoryProposal(text=f"Polaris check {check_id} fails: {message}"),
                    algorithm=algorithm,
                    evidence=SignalEvidence(summary=f"Polaris ({severity}) on {label}: {message}", data=data),
                    confidence=SEVERITY_CONFIDENCE.get(severity),
                    provenance=SignalProvenance(origin=SignalOrigin.PUSH),
                    raw=trim_raw({"check": dict(check), "kind": kind, "namespace": namespace, "name": name}),
                    fetched_at=received_at,
                )
                if signal.signal_id in seen:
                    count("duplicate")
                    continue
                seen.add(signal.signal_id)
                result.signals.append(signal)
    messages = {
        "invalid_entry": "entries without a readable Kind and Name were skipped",
        "passed": "checks that pass were not reported",
        "ignored_severity": "failed checks of severity 'ignore' were not reported",
        "duplicate": "duplicate findings were merged",
    }
    for code, amount in sorted(counts.items()):
        level = "warning" if code == "invalid_entry" else "info"
        result.diagnostics.append(Diagnostic(level=level, code=code, message=messages[code], count=amount))
    return result


class PolarisSource(InboxSource):
    """Fairwinds Polaris findings pushed as JSON reports, with native follow-ups (roadmap G4)."""

    id: ClassVar[str] = POLARIS_SOURCE_ID
    display_name: ClassVar[str] = "Fairwinds Polaris"
    tier: ClassVar[SourceTier] = SourceTier.OSS
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.HYGIENE, Capability.CONTAINER_REQUESTS})
    description: ClassVar[str] = (
        "Hygiene findings from Polaris audit reports pushed to POST /api/v1/sources/polaris/reports (from CI or "
        "the optional CronJob); missing requests get a request computed from observed usage."
    )
    inbox_source_id: ClassVar[Optional[str]] = POLARIS_SOURCE_ID
    push_hint: ClassVar[str] = (
        "POST 'polaris audit --format=json' output to /api/v1/sources/polaris/reports with a signals:write API key, "
        "or enable sources.polaris.job in the Helm chart"
    )

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        batch = await super().collect(ctx)
        follow_ups, skipped = self.follow_ups(batch.signals, ctx)
        batch.signals.extend(follow_ups)
        if skipped:
            batch.diagnostics.append(
                Diagnostic(
                    code="follow_up_without_usage",
                    message="missing-request findings without enough observed usage (or whose container now has "
                    "a request) got no follow-up proposal",
                    count=skipped,
                )
            )
        return batch

    def follow_ups(self, findings: List[SourceSignal], ctx: CollectContext) -> Tuple[List[SourceSignal], int]:
        """Request proposals for missing-request findings, computed from GreenKube's usage data."""
        context = ctx.optimization
        config = self.deps.config
        headroom = float(getattr(config, "RIGHTSIZING_HEADROOM", 1.2) or 1.2)
        min_samples = int(getattr(config, "RECOMMENDATION_MIN_SAMPLES", 36) or 0)
        minimum = {
            "cpu": int(getattr(config, "RECOMMENDATION_MIN_CPU_MILLICORES", 10) or 0),
            "memory": int(getattr(config, "RECOMMENDATION_MIN_MEMORY_BYTES", 16 * _MIB) or 0),
        }
        proposals: List[SourceSignal] = []
        skipped = 0
        done: set[Tuple[str, str]] = set()
        for finding in findings:
            check = finding.evidence.data.get("check")
            resource = MISSING_REQUEST_CHECKS.get(str(check))
            target = finding.target
            if resource is None or not target.container or not target.namespace:
                continue
            if (target.key(), resource) in done:
                continue
            done.add((target.key(), resource))
            summary = None
            if context is not None:
                for container, candidate in context.container_summaries((target.namespace, target.kind, target.name)):
                    if container == target.container:
                        summary = candidate
                        break
            stats = getattr(summary, resource, None) if summary is not None else None
            current = (summary.cpu_request if resource == "cpu" else summary.memory_request) if summary else 0
            if stats is None or stats.sample_count < min_samples or current:
                skipped += 1
                continue
            value = max(int(round(stats.p95 * headroom)), minimum[resource])
            spec = ResourceSpec(cpu_request_m=value) if resource == "cpu" else ResourceSpec(memory_request_bytes=value)
            shown = f"{value}m" if resource == "cpu" else f"{value / _MIB:.0f}Mi"
            label = f"{target.kind} {target.namespace}/{target.name} container '{target.container}'"
            proposals.append(
                SourceSignal(
                    source_id=POLARIS_SOURCE_ID,
                    source_ref=finding.source_ref,
                    engine_family=FOLLOW_UP_ENGINE,
                    target=target,
                    capability=Capability.CONTAINER_REQUESTS,
                    kind="RIGHTSIZING_CPU" if resource == "cpu" else "RIGHTSIZING_MEMORY",
                    proposal=ContainerResourcesProposal(container=target.container, proposed=spec),
                    algorithm=AlgorithmInfo(
                        name=FOLLOW_UP_ALGORITHM,
                        params={"percentile": 95, "headroom": headroom},
                        window=ctx.window,
                    ),
                    evidence=SignalEvidence(
                        summary=(
                            f"Polaris reports no {resource} request on {label}; GreenKube proposes {shown} "
                            f"(p95 of observed usage x {headroom:g})."
                        ),
                        data={
                            "polaris_check": check,
                            "polaris_signal_id": finding.signal_id,
                            "p95": stats.p95,
                            "samples": stats.sample_count,
                            "hygiene_follow_up": True,
                        },
                    ),
                    observed_window=ctx.window,
                    provenance=SignalProvenance(origin=SignalOrigin.NATIVE),
                    fetched_at=self.deps.clock(),
                )
            )
        return proposals, skipped
