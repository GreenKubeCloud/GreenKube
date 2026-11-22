"""The example third-party package registers through its entry point and passes the contract (E5)."""

from __future__ import annotations

import json
import tomllib
from datetime import datetime, timedelta, timezone
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.registry import build_signal_sources, build_source_registry
from greenkube.signals.sdk import ENTRY_POINT_GROUP, CollectContext, SourceDeps
from greenkube.signals.testing import check_source_contract

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "greenkube-source-example"


@pytest.fixture
def entry_points(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE / "src"))
    project = tomllib.loads((EXAMPLE / "pyproject.toml").read_text())
    declared = project["project"]["entry-points"][ENTRY_POINT_GROUP]
    return [EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP) for name, value in declared.items()]


def test_the_declared_entry_point_registers_a_plugin(entry_points):
    registry, configured = build_source_registry(Config(), entry_points=lambda: entry_points)
    assert registry.problems == []
    example = configured[-1]
    assert example.id == "example.file"
    assert example.descriptor.origin == "plugin"


async def test_the_example_passes_the_contract_with_configured_settings(entry_points, tmp_path):
    path = tmp_path / "recommendations.json"
    path.write_text(
        json.dumps(
            [
                {
                    "namespace": "shop",
                    "kind": "Deployment",
                    "name": "api",
                    "container": "web",
                    "current": {"cpu_request_m": 500},
                    "proposed": {"cpu_request_m": 250},
                }
            ]
        )
    )
    config = Config().model_copy(update={"SOURCES_CONFIG": json.dumps({"example.file": {"path": str(path)}})})
    [source] = [
        s for s in build_signal_sources(config, entry_points=lambda: entry_points) if s.source_id == "example.file"
    ]
    ctx = CollectContext(
        cluster_uid="c1", namespace=None, window=None, deadline=datetime.now(timezone.utc) + timedelta(minutes=1)
    )
    assert isinstance(source.deps, SourceDeps)
    await check_source_contract(source, ctx, builtin=False)
