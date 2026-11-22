"""Push protocol: public schema, batch validation with JSON pointers and the push source (roadmap G6)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from greenkube.core.config import Config
from greenkube.signals.connectors.inbox import InboxBatch, InboxSettings
from greenkube.signals.connectors.push import (
    BATCH_SCHEMA_FILE,
    SIGNAL_SCHEMA_FILE,
    PushSignal,
    PushSignalSource,
    batch_schema,
    json_pointer,
    parse_batch,
    render,
    signal_schema,
)
from greenkube.signals.model import ContainerResourcesProposal, SignalOrigin, SourceSignal
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.sdk import Diagnostic, SourceDeps, SourceHealthStatus, SourceSettings
from tests.sources.contract import CLUSTER, SourceContract, collect_context  # pyrefly: ignore[missing-import]
from tests.sources.fake_inbox import MemoryInbox  # pyrefly: ignore[missing-import]

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
MIB = 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _signal(**overrides) -> dict:
    signal = {
        "target": {"kind": "Deployment", "namespace": "shop", "name": "api", "container": "web"},
        "capability": "container_requests",
        "kind": "RIGHTSIZING_CPU",
        "proposal": {
            "kind": "container_resources",
            "container": "web",
            "current": {"cpu_request_m": 1000},
            "proposed": {"cpu_request_m": 300},
        },
        "algorithm": {"name": "acme.p95", "params": {"percentile": 95}},
        "evidence": {"summary": "p95 CPU is 210m over 14 days"},
        "confidence": 0.7,
    }
    signal.update(overrides)
    return signal


def _batch(*signals, complete=True, source_id="push:ci") -> dict:
    return {"source_id": source_id, "complete": complete, "signals": list(signals) or [_signal()]}


class TestSchemaFiles:
    @pytest.mark.parametrize(
        ("name", "factory"), [(SIGNAL_SCHEMA_FILE, signal_schema), (BATCH_SCHEMA_FILE, batch_schema)]
    )
    def test_published_schema_matches_the_model(self, name, factory):
        published = (SCHEMAS / name).read_text(encoding="utf-8")
        assert published == render(factory()), (
            f"schemas/{name} is stale: run python -m greenkube.signals.connectors.push schemas"
        )

    def test_signal_schema_leaves_out_server_computed_fields(self):
        properties = signal_schema()["properties"]
        for computed in ("signal_id", "schema_version", "source_id", "provenance", "fetched_at"):
            assert computed not in properties
        assert {"target", "capability", "kind", "proposal", "algorithm"} <= set(signal_schema()["required"])
        assert signal_schema()["$id"].endswith("signal.v1alpha1.json")

    def test_push_signal_mirrors_source_signal_minus_server_fields(self):
        """A new SourceSignal field must be added to the push schema too (or declared server-computed)."""
        server_computed = {"schema_version", "signal_id", "source_id", "provenance", "fetched_at"}
        assert set(PushSignal.model_fields) == set(SourceSignal.model_fields) - server_computed


class TestParseBatch:
    def test_valid_batch_becomes_push_signals_of_this_cluster(self):
        received = _now()
        batch, signals, errors = parse_batch(_batch(), cluster_uid=CLUSTER, received_at=received)

        assert errors == [] and batch is not None and batch.complete is True
        [signal] = signals
        assert signal.source_id == "push:ci"
        assert signal.target.cluster_uid == CLUSTER and signal.target.api_version == "apps/v1"
        assert signal.provenance.origin == SignalOrigin.PUSH
        assert signal.fetched_at == received
        assert isinstance(signal.proposal, ContainerResourcesProposal) and signal.proposal.direction == "decrease"

    def test_signal_id_is_stable_across_pushes(self):
        first = parse_batch(_batch(), cluster_uid=CLUSTER, received_at=_now())[1][0]
        evidence = {"summary": "a new explanation"}
        second = parse_batch(_batch(_signal(evidence=evidence)), cluster_uid=CLUSTER, received_at=_now())[1][0]
        assert first.signal_id == second.signal_id

    def test_errors_point_into_the_document(self):
        document = _batch(
            _signal(),
            _signal(proposal={"kind": "container_resources", "proposed": {"cpu_request_m": -5}}),
            _signal(capability="teleportation", unexpected=True),
        )
        batch, signals, errors = parse_batch(document, cluster_uid=CLUSTER, received_at=_now())

        assert batch is None and signals == []
        pointers = {error.pointer for error in errors}
        assert "/signals/1/proposal/proposed/cpu_request_m" in pointers
        assert "/signals/2/capability" in pointers
        assert "/signals/2/unexpected" in pointers

    def test_source_id_must_be_a_push_client(self):
        _batch_, _signals, errors = parse_batch(_batch(source_id="vpa"), cluster_uid=CLUSTER, received_at=_now())
        assert [error.pointer for error in errors] == ["/source_id"]

    def test_complete_flag_is_required(self):
        document = _batch()
        del document["complete"]
        _batch_, _signals, errors = parse_batch(document, cluster_uid=CLUSTER, received_at=_now())
        assert [error.pointer for error in errors] == ["/complete"]

    def test_signal_about_another_cluster_is_rejected(self):
        other = _signal(target={"cluster_uid": "elsewhere", "kind": "Deployment", "namespace": "a", "name": "b"})
        _batch_, signals, errors = parse_batch(_batch(_signal(), other), cluster_uid=CLUSTER, received_at=_now())
        assert signals == []
        assert [(error.pointer, error.type) for error in errors] == [
            ("/signals/1/target/cluster_uid", "unknown_cluster")
        ]

    def test_namespaced_kinds_need_a_namespace(self):
        no_namespace = _signal(target={"kind": "Deployment", "name": "api"})
        node = _signal(
            target={"kind": "NodePool", "name": "general"},
            capability="node_pool",
            kind="NODEPOOL_ARCHITECTURE",
            proposal={"kind": "advisory", "text": "allow arm64"},
        )
        _batch_, signals, errors = parse_batch(_batch(no_namespace), cluster_uid=CLUSTER, received_at=_now())
        assert signals == [] and [e.pointer for e in errors] == ["/signals/0/target/namespace"]
        batch, signals, errors = parse_batch(_batch(node), cluster_uid=CLUSTER, received_at=_now())
        assert errors == [] and signals[0].target.namespace is None

    def test_pointer_escapes_tokens(self):
        assert json_pointer({"a/b": {"c~d": 1}}, ("a/b", "c~d")) == "/a~1b/c~0d"
        assert json_pointer({"signals": []}, ("signals", 3, "kind")) == "/signals/3/kind"


def _push_source(inbox: MemoryInbox, clock=None, **settings) -> PushSignalSource:
    deps = SourceDeps(config=Config(), cluster_uid=CLUSTER, clock=clock or _now)
    return PushSignalSource(InboxSettings(**settings), deps, inbox=inbox)


async def _accept(inbox: MemoryInbox, document: dict, received_at: datetime, batch_id: str = "b1"):
    batch, signals, errors = parse_batch(document, cluster_uid=CLUSTER, received_at=received_at)
    assert not errors and batch is not None
    return await inbox.accept(
        InboxBatch(
            batch_id=batch_id,
            source_id=batch.source_id,
            received_at=received_at,
            complete=batch.complete,
            signal_count=len(signals),
            diagnostics=[Diagnostic(code="x", message="y")],
        ),
        signals,
    )


class TestPushSource:
    async def test_unavailable_until_something_is_pushed(self):
        report = await SourceRunner().run(_push_source(MemoryInbox()), collect_context(None))
        assert report.run.status == SourceRunStatus.UNAVAILABLE
        assert "/api/v1/signals" in (report.run.health_reason or "")

    async def test_replays_pushed_signals_with_their_client_source_id(self):
        inbox = MemoryInbox()
        await _accept(inbox, _batch(), _now())
        await _accept(inbox, _batch(source_id="push:acme-bot"), _now(), batch_id="b2")

        report = await SourceRunner().run(_push_source(inbox), collect_context(None))

        assert report.run.status == SourceRunStatus.SUCCEEDED
        assert sorted(signal.source_id for signal in report.signals) == ["push:acme-bot", "push:ci"]
        assert report.recommendations == []

    async def test_signals_older_than_max_age_are_no_longer_used(self):
        inbox = MemoryInbox()
        await _accept(inbox, _batch(), _now() - timedelta(hours=10))

        report = await SourceRunner().run(_push_source(inbox, max_age_hours=2), collect_context(None))

        assert report.run.status == SourceRunStatus.SUCCEEDED
        assert report.run.health_status == SourceHealthStatus.DEGRADED
        assert "older than max_age_hours" in (report.run.health_reason or "")
        assert report.signals == []

    async def test_namespaced_scan_replays_only_its_namespace(self):
        inbox = MemoryInbox()
        other = _signal(target={"kind": "Deployment", "namespace": "billing", "name": "worker"})
        await _accept(inbox, _batch(_signal(), other), _now())

        batch = await _push_source(inbox).collect(collect_context(None, namespace="billing"))

        assert [signal.target.namespace for signal in batch.signals] == ["billing"]

    async def test_complete_batch_withdraws_earlier_signals(self):
        inbox = MemoryInbox()
        await _accept(
            inbox,
            _batch(
                _signal(),
                _signal(
                    kind="RIGHTSIZING_MEMORY",
                    proposal={"kind": "container_resources", "proposed": {"memory_request_bytes": 256 * MIB}},
                ),
            ),
            _now(),
        )
        stored = await _accept(inbox, _batch(_signal()), _now(), batch_id="b2")

        assert stored.withdrawn == 1
        batch = await _push_source(inbox).collect(collect_context(None))
        assert [signal.kind for signal in batch.signals] == ["RIGHTSIZING_CPU"]

    async def test_targets_without_usage_are_kept_and_reported(self):
        from greenkube.core.optimization.context import OptimizationContext
        from greenkube.models.metrics import CombinedMetric

        inbox = MemoryInbox()
        await _accept(inbox, _batch(), _now())
        metric = CombinedMetric(
            pod_name="worker-1",
            namespace="shop",
            owner_kind="Deployment",
            owner_name="worker",
            container_name="main",
            cpu_request=100,
            timestamp=_now() - timedelta(hours=1),
        )
        context = OptimizationContext(config=Config(), metrics=[metric])

        batch = await _push_source(inbox).collect(collect_context(context))

        assert len(batch.signals) == 1
        assert {d.code: d.count for d in batch.diagnostics} == {"targets_without_usage": 1}

    def test_settings_reject_unknown_keys(self):
        with pytest.raises(ValueError):
            InboxSettings.model_validate({"max_age_hours": 5, "typo": 1})
        assert isinstance(InboxSettings(), SourceSettings)


class TestPushSourceContract(SourceContract):
    def make_source(self):
        inbox = MemoryInbox()
        received = _now()
        _batch_, signals, _errors = parse_batch(_batch(), cluster_uid=CLUSTER, received_at=received)
        inbox.seed(InboxBatch("b1", "push:ci", received, True, len(signals)), signals)
        return _push_source(inbox)

    def make_context(self):
        return collect_context(None)


def test_pushed_signal_round_trips_through_json():
    signal = parse_batch(_batch(), cluster_uid=CLUSTER, received_at=_now())[1][0]
    again = SourceSignal.model_validate(json.loads(signal.model_dump_json()))
    assert again == signal
