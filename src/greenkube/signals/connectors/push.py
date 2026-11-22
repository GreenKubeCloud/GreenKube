"""Push protocol: any tool contributes signals over HTTP (roadmap G6).

``POST /api/v1/signals`` takes a batch::

    {"source_id": "push:<client>", "complete": true, "signals": [<signal.v1alpha1>, ...]}

A pushed signal is a :class:`~greenkube.signals.model.SourceSignal` minus what
the server computes: ``schema_version``, ``signal_id``, ``source_id`` (the
batch's), ``provenance`` (``origin: push``) and ``fetched_at`` (the time the
batch was received). ``target.cluster_uid`` may be left out: it defaults to this
GreenKube's cluster. The public JSON Schemas ``schemas/signal.v1alpha1.json``
and ``schemas/signal-batch.v1alpha1.json`` are generated from the models below
(``python -m greenkube.signals.connectors.push schemas``); a test keeps them in
sync.

Validation errors carry RFC 6901 JSON pointers into the request document
(``/signals/3/proposal/proposed/cpu_request_m``).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from greenkube.signals.connectors.inbox import InboxSource
from greenkube.signals.model import (
    CLUSTER_SCOPED_KINDS,
    KIND_API_VERSIONS,
    AlgorithmInfo,
    Capability,
    Proposal,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
    TimeWindow,
    missing_resource_value,
    trim_raw,
)
from greenkube.signals.sdk import SourceTier

SIGNAL_SCHEMA_ID = "https://greenkube.io/schemas/signal.v1alpha1.json"
BATCH_SCHEMA_ID = "https://greenkube.io/schemas/signal-batch.v1alpha1.json"
SIGNAL_SCHEMA_FILE = "signal.v1alpha1.json"
BATCH_SCHEMA_FILE = "signal-batch.v1alpha1.json"

#: Source ids of pushed batches: ``push:`` and a lowercase client name.
PUSH_PREFIX = "push:"
PUSH_SOURCE_PATTERN = r"^push:[a-z0-9][a-z0-9_.-]{0,62}$"
#: Signals accepted in one batch.
MAX_BATCH_SIGNALS = 5000


class PushTarget(BaseModel):
    """Object the signal is about. ``cluster_uid`` defaults to this GreenKube's cluster."""

    model_config = ConfigDict(extra="forbid")

    cluster_uid: Optional[str] = Field(None, min_length=1, max_length=253)
    #: Filled for well-known kinds (``apps/v1`` for a Deployment) when left out.
    api_version: Optional[str] = Field(None, max_length=128)
    kind: str = Field(..., min_length=1, max_length=128)
    namespace: Optional[str] = Field(None, min_length=1, max_length=253)
    name: str = Field(..., min_length=1, max_length=253)
    container: Optional[str] = Field(None, min_length=1, max_length=253)


class PushSignal(BaseModel):
    """One pushed signal (``signal.v1alpha1``): a SourceSignal without its server-computed fields."""

    model_config = ConfigDict(extra="forbid", title="GreenKube signal v1alpha1")

    target: PushTarget
    capability: Capability
    #: Finding kind (``RIGHTSIZING_CPU``, ``RIGHTSIZING_MEMORY``, ``ORPHANED_PERSISTENT_VOLUME``...).
    kind: str = Field(..., min_length=1, max_length=128)
    proposal: Proposal
    algorithm: AlgorithmInfo
    evidence: SignalEvidence = SignalEvidence()
    observed_window: Optional[TimeWindow] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    #: Savings the client claims; kept for comparison, never used for ranking.
    source_estimate: Optional[Dict[str, Any]] = None
    source_ref: Optional[str] = Field(None, max_length=512)
    source_version: Optional[str] = Field(None, max_length=64)
    #: Engine behind the advice when several clients relay the same one (consensus counts one vote).
    engine_family: Optional[str] = Field(None, min_length=1, max_length=64)
    #: Audit payload, trimmed to 8 KiB.
    raw: Dict[str, Any] = Field(default_factory=dict)


class SignalBatch(BaseModel):
    """One push (``signal-batch.v1alpha1``).

    ``complete: true`` declares the batch the client's full current advice: its
    signals replace every signal the client pushed before. ``complete: false``
    adds or refreshes signals and keeps the others.
    """

    model_config = ConfigDict(extra="forbid", title="GreenKube signal batch v1alpha1")

    source_id: str = Field(..., pattern=PUSH_SOURCE_PATTERN)
    complete: bool
    signals: List[PushSignal] = Field(..., max_length=MAX_BATCH_SIGNALS)


class PointerError(BaseModel):
    """One validation error located by a JSON pointer into the request document."""

    pointer: str
    message: str
    type: str


def _escape(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def json_pointer(document: Any, loc: Sequence[Any]) -> str:
    """RFC 6901 pointer of a pydantic error location, walking the actual ``document``.

    Pydantic adds the discriminator value of a tagged union to the location
    (``proposal/container_resources/proposed``); segments that do not exist in
    the document are left out of the pointer.
    """
    tokens: List[str] = []
    node = document
    for segment in loc:
        if isinstance(node, dict) and isinstance(segment, str):
            if segment in node:
                tokens.append(_escape(segment))
                node = node[segment]
                continue
            if node.get("kind") == segment:
                # Tagged-union branch name, not a document member.
                continue
            tokens.append(_escape(segment))
            node = None
            continue
        if isinstance(node, list) and isinstance(segment, int):
            tokens.append(str(segment))
            node = node[segment] if 0 <= segment < len(node) else None
            continue
        if isinstance(segment, (str, int)):
            tokens.append(_escape(segment))
        node = None
    return "/" + "/".join(tokens) if tokens else ""


def pointer_errors(document: Any, error: ValidationError, *, prefix: Sequence[Any] = ()) -> List[PointerError]:
    return [
        PointerError(
            pointer=json_pointer(document, (*prefix, *item["loc"])),
            message=item["msg"],
            type=item["type"],
        )
        for item in error.errors(include_url=False)
    ]


def to_source_signal(pushed: PushSignal, *, source_id: str, cluster_uid: str, received_at: datetime) -> SourceSignal:
    """The stored signal of a pushed one (origin ``push``, this cluster, raw trimmed)."""
    target = pushed.target
    return SourceSignal(
        source_id=source_id,
        source_version=pushed.source_version,
        source_ref=pushed.source_ref,
        engine_family=pushed.engine_family,
        target=TargetRef.for_kind(
            target.cluster_uid or cluster_uid,
            target.kind,
            target.name,
            namespace=target.namespace,
            container=target.container,
            api_version=target.api_version,
        ),
        capability=pushed.capability,
        kind=pushed.kind,
        proposal=pushed.proposal,
        algorithm=pushed.algorithm,
        evidence=pushed.evidence,
        observed_window=pushed.observed_window,
        confidence=pushed.confidence,
        source_estimate=pushed.source_estimate,
        provenance=SignalProvenance(origin=SignalOrigin.PUSH),
        raw=trim_raw(pushed.raw),
        fetched_at=received_at,
    )


def parse_batch(
    document: Any, *, cluster_uid: str, received_at: datetime
) -> Tuple[Optional[SignalBatch], List[SourceSignal], List[PointerError]]:
    """Validate a pushed batch; on error, every problem with its JSON pointer and no signal."""
    try:
        batch = SignalBatch.model_validate(document)
    except ValidationError as exc:
        return None, [], pointer_errors(document, exc)
    errors: List[PointerError] = []
    for index, pushed in enumerate(batch.signals):
        kind = pushed.target.kind
        if kind in KIND_API_VERSIONS and kind not in CLUSTER_SCOPED_KINDS and not pushed.target.namespace:
            errors.append(
                PointerError(
                    pointer=f"/signals/{index}/target/namespace",
                    message=f"a {kind} target needs its namespace",
                    type="missing_namespace",
                )
            )
        missing = missing_resource_value(pushed.capability, pushed.proposal, pushed.kind)
        if missing is not None:
            errors.append(
                PointerError(
                    pointer=f"/signals/{index}/proposal"
                    + ("/proposed" if getattr(pushed.proposal, "kind", None) == "container_resources" else ""),
                    message=missing,
                    type="missing_resource_value",
                )
            )
        uid = pushed.target.cluster_uid
        if uid is not None and uid != cluster_uid:
            errors.append(
                PointerError(
                    pointer=f"/signals/{index}/target/cluster_uid",
                    message=f"this GreenKube manages cluster {cluster_uid!r}; push to the GreenKube of {uid!r}",
                    type="unknown_cluster",
                )
            )
    if errors:
        return None, [], errors
    signals = [
        to_source_signal(pushed, source_id=batch.source_id, cluster_uid=cluster_uid, received_at=received_at)
        for pushed in batch.signals
    ]
    return batch, signals, []


def _schema(model: type[BaseModel], schema_id: str, description: str) -> Dict[str, Any]:
    body = model.model_json_schema(mode="validation")
    schema: Dict[str, Any] = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": schema_id}
    schema.update(body)
    schema["description"] = description
    return schema


def signal_schema() -> Dict[str, Any]:
    return _schema(
        PushSignal,
        SIGNAL_SCHEMA_ID,
        "One signal pushed to GreenKube (POST /api/v1/signals): a SourceSignal without the fields the "
        "server computes (schema_version, signal_id, source_id, provenance, fetched_at).",
    )


def batch_schema() -> Dict[str, Any]:
    return _schema(
        SignalBatch,
        BATCH_SCHEMA_ID,
        "Body of POST /api/v1/signals: a batch of signals from one push client. complete=true replaces "
        "every signal the client pushed before.",
    )


def render(schema: Dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_schemas(directory: Path) -> List[Path]:
    """Write both public schemas into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, schema in ((SIGNAL_SCHEMA_FILE, signal_schema()), (BATCH_SCHEMA_FILE, batch_schema())):
        path = directory / name
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written


class PushSignalSource(InboxSource):
    """Signals pushed by any tool through ``POST /api/v1/signals`` (roadmap G6)."""

    id: ClassVar[str] = "push"
    display_name: ClassVar[str] = "Push API"
    tier: ClassVar[SourceTier] = SourceTier.CUSTOM
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset(Capability)
    also_emits: ClassVar[FrozenSet[str]] = frozenset({PUSH_PREFIX})
    description: ClassVar[str] = (
        "Signals pushed by external tools to POST /api/v1/signals (source ids push:<client>); "
        "they become recommendations only through consensus."
    )
    inbox_prefix: ClassVar[Optional[str]] = PUSH_PREFIX
    push_hint: ClassVar[str] = "POST a signal batch to /api/v1/signals with a signals:write API key"


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    for written_path in write_schemas(Path(sys.argv[1] if len(sys.argv) > 1 else "schemas")):
        print(written_path)
