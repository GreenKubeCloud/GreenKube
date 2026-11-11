"""The example source passes GreenKube's source contract.

Run from the GreenKube repository: ``uv run pytest examples/greenkube-source-example/tests``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from greenkube_source_example import FileSource, FileSourceSettings

from greenkube.core.config import Config
from greenkube.signals import CollectContext, ContainerResourcesProposal, SourceDeps, TargetRef, WorkloadInventory
from greenkube.signals.runtime import SourceRunner, SourceRunStatus
from greenkube.signals.testing import check_source_contract

CLUSTER = "example"


def _ctx(inventory=()) -> CollectContext:
    return CollectContext(
        cluster_uid=CLUSTER,
        namespace=None,
        window=None,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
        inventory=WorkloadInventory(targets=list(inventory)),
    )


def _source(path) -> FileSource:
    return FileSource(FileSourceSettings(path=str(path)), SourceDeps(config=Config(), cluster_uid=CLUSTER))


ENTRY = {
    "namespace": "shop",
    "kind": "Deployment",
    "name": "api",
    "container": "web",
    "current": {"cpu_request_m": 500},
    "proposed": {"cpu_request_m": 250},
    "confidence": 0.8,
}


async def test_contract(tmp_path):
    path = tmp_path / "recommendations.json"
    path.write_text(json.dumps([ENTRY]))
    empty = tmp_path / "empty.json"
    empty.write_text("[]")

    batch = await check_source_contract(_source(path), _ctx(), builtin=False)

    [signal] = batch.signals
    assert isinstance(signal.proposal, ContainerResourcesProposal)
    assert signal.proposal.direction == "decrease"
    assert signal.target.key() == "example/apps.Deployment/shop/api/web"
    assert (await _source(empty).collect(_ctx())).signals == []


async def test_missing_file_is_unavailable_not_failed(tmp_path):
    report = await SourceRunner().run(_source(tmp_path / "absent.json"), _ctx())
    assert report.run.status == SourceRunStatus.UNAVAILABLE


async def test_unreadable_entries_make_the_result_partial(tmp_path):
    path = tmp_path / "recommendations.json"
    path.write_text(json.dumps([ENTRY, {"kind": "Deployment"}]))
    report = await SourceRunner().run(_source(path), _ctx())
    assert report.run.status == SourceRunStatus.INCOMPLETE
    assert report.run.signal_count == 1


async def test_unknown_workloads_are_ignored(tmp_path):
    path = tmp_path / "recommendations.json"
    path.write_text(json.dumps([ENTRY, {**ENTRY, "name": "deleted"}]))
    known = TargetRef.for_kind(CLUSTER, "Deployment", "api", namespace="shop", container="web")
    batch = await _source(path).collect(_ctx([known]))
    assert [s.target.name for s in batch.signals] == ["api"]
    assert [d.code for d in batch.diagnostics] == ["unknown_target"]
