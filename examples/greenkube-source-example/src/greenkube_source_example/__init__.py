"""Example third-party GreenKube source: container request proposals read from a JSON file.

It shows the whole Source SDK contract (``docs/sources.md``) on the simplest
possible backend, a file written by another tool:

* metadata (id, display name, tier, capabilities, version, limits);
* a settings model validated from ``SOURCES_CONFIG["example.file"]``;
* ``discover()`` (is the file there?), ``health()`` and ``collect()``;
* diagnostics and ``complete=False`` when part of the input is unusable.

Installed with ``pip install``, it registers itself through the
``greenkube.sources`` entry point declared in ``pyproject.toml`` and appears in
``GET /api/v1/sources``.

File format (a JSON array)::

    [{"namespace": "shop", "kind": "Deployment", "name": "api", "container": "web",
      "current": {"cpu_request_m": 500, "memory_request_bytes": 536870912},
      "proposed": {"cpu_request_m": 250, "memory_request_bytes": 268435456},
      "confidence": 0.8}]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional

from pydantic import Field, ValidationError

from greenkube.signals import (
    AlgorithmInfo,
    Capability,
    CollectContext,
    ContainerResourcesProposal,
    Diagnostic,
    ResourceSpec,
    SignalEvidence,
    Source,
    SourceBatch,
    SourceHealth,
    SourceLimits,
    SourceSettings,
    SourceSignal,
    SourceTier,
    TargetRef,
)

__version__ = "0.1.0"


class FileSourceSettings(SourceSettings):
    """``SOURCES_CONFIG["example.file"]``; ``enabled`` comes from the base model."""

    path: str = "/etc/greenkube/example-recommendations.json"
    #: Ignore proposals for workloads GreenKube does not know (typos, deleted workloads).
    require_known_targets: bool = True
    max_entries: int = Field(10_000, ge=1)


class FileSource(Source):
    """Container request proposals read from a JSON file written by another tool."""

    id: ClassVar[str] = "example.file"
    display_name: ClassVar[str] = "Example: proposals from a JSON file"
    description: ClassVar[str] = "Reads container request proposals from a JSON file (Source SDK example)."
    tier: ClassVar[SourceTier] = SourceTier.CUSTOM
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    version: ClassVar[Optional[str]] = __version__
    settings_model: ClassVar[type[SourceSettings]] = FileSourceSettings
    limits: ClassVar[SourceLimits] = SourceLimits(probe_timeout_seconds=5, collect_timeout_seconds=30)

    @property
    def path(self) -> Path:
        settings = self.settings
        assert isinstance(settings, FileSourceSettings)
        return Path(settings.path)

    async def discover(self) -> SourceHealth:
        if self.path.is_file():
            return SourceHealth.available()
        return SourceHealth.unavailable(f"{self.path} does not exist")

    async def health(self) -> SourceHealth:
        try:
            self.path.stat()
        except OSError as exc:
            return SourceHealth.unavailable(type(exc).__name__)
        return SourceHealth.available()

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        settings = self.settings
        assert isinstance(settings, FileSourceSettings)
        entries = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("the file must contain a JSON array")

        signals: List[SourceSignal] = []
        invalid = unknown = 0
        fetched_at = self.deps.clock()
        for entry in entries[: settings.max_entries]:
            try:
                signal = self._signal(entry, ctx, fetched_at)
            except (ValidationError, KeyError, TypeError, ValueError):
                invalid += 1
                continue
            if ctx.namespace is not None and signal.target.namespace != ctx.namespace:
                continue
            if settings.require_known_targets and ctx.inventory.targets and signal.target not in ctx.inventory.targets:
                unknown += 1
                continue
            signals.append(signal)

        diagnostics: List[Diagnostic] = []
        if invalid:
            diagnostics.append(
                Diagnostic(level="error", code="invalid_entry", message="unreadable entries", count=invalid)
            )
        if unknown:
            diagnostics.append(
                Diagnostic(code="unknown_target", message="entries for unknown workloads", count=unknown)
            )
        truncated = len(entries) > settings.max_entries
        if truncated:
            diagnostics.append(
                Diagnostic(level="warning", code="truncated", message="entries over max_entries ignored")
            )
        # Unreadable or ignored entries make the result partial: GreenKube then
        # keeps this source's previous advice instead of treating it as resolved.
        return SourceBatch(signals=signals, complete=not invalid and not truncated, diagnostics=diagnostics)

    def _signal(self, entry: Dict[str, Any], ctx: CollectContext, fetched_at) -> SourceSignal:
        target = TargetRef.for_kind(
            ctx.cluster_uid,
            str(entry["kind"]),
            str(entry["name"]),
            namespace=str(entry["namespace"]),
            container=str(entry["container"]),
        )
        proposal = ContainerResourcesProposal(
            container=target.container,
            current=ResourceSpec.model_validate(entry.get("current") or {}),
            proposed=ResourceSpec.model_validate(entry["proposed"]),
        )
        return SourceSignal(
            source_id=self.source_id,
            source_version=__version__,
            source_ref=str(self.path),
            target=target,
            capability=Capability.CONTAINER_REQUESTS,
            kind="RIGHTSIZING",
            proposal=proposal,
            algorithm=AlgorithmInfo(name="example.file"),
            evidence=SignalEvidence(summary=f"Proposal read from {self.path.name}"),
            observed_window=ctx.window,
            confidence=entry.get("confidence"),
            raw={key: entry[key] for key in ("current", "proposed") if key in entry},
            fetched_at=fetched_at,
        )


__all__ = ["FileSource", "FileSourceSettings"]
