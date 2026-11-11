"""Source registry: built-ins, entry-point plugins and SOURCES_CONFIG (roadmap E5)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar, FrozenSet

import pytest
from pydantic import ValidationError

from greenkube.core.config import Config
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.registry import build_signal_sources, build_source_registry
from greenkube.signals.model import Capability
from greenkube.signals.registry import parse_sources_config
from greenkube.signals.sdk import EnabledMode, Source, SourceBatch, SourceSettings, SourceTier

NATIVE_IDS = [
    "greenkube.zombie",
    "greenkube.rightsizing_cpu",
    "greenkube.rightsizing_memory",
    "greenkube.autoscaling",
    "greenkube.off_peak",
    "greenkube.idle_namespace",
    "greenkube.carbon_aware",
    "greenkube.nodes",
    "greenkube.orphaned_pv",
    "greenkube.orphaned_lb",
]


class AcmeSettings(SourceSettings):
    url: str = "http://acme"
    window_days: int = 7


class AcmeSource(Source):
    id: ClassVar[str] = "acme"
    display_name: ClassVar[str] = "Acme recommender"
    tier: ClassVar[SourceTier] = SourceTier.COMMERCIAL
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    settings_model: ClassVar[type[SourceSettings]] = AcmeSettings

    async def collect(self, ctx):
        return SourceBatch()


class Squatter(AcmeSource):
    id: ClassVar[str] = "greenkube.fake"


class Duplicate(AcmeSource):
    display_name: ClassVar[str] = "Another Acme"


def _entry_point(name: str, loader, dist: str = "acme-greenkube-source"):
    return SimpleNamespace(name=name, value=f"{dist}:{name}", load=loader, dist=SimpleNamespace(name=dist))


def _boom():
    raise ImportError("No module named 'acme_sdk'")


def _config(**overrides) -> Config:
    return Config().model_copy(update=overrides)


class TestBuiltins:
    def test_builtins_in_execution_order(self):
        registry, configured = build_source_registry(Config(), entry_points=lambda: [])
        assert [item.id for item in configured] == ["vpa", "greenkube.nodepool", *NATIVE_IDS]
        assert all(item.descriptor.origin == "builtin" for item in configured)
        assert registry.problems == []

    def test_engine_runs_the_sdk_sources_by_default(self):
        engine = OptimizationEngine(config=Config(), sources=build_signal_sources(Config(), entry_points=lambda: []))
        assert [source.source_id for source in engine.sources] == ["vpa", "greenkube.nodepool", *NATIVE_IDS]
        # v1 run records still count three sources: vpa, karpenter and greenkube.
        assert engine.source_group_count == 3

    def test_legacy_flags_disable_vpa_and_the_nodepool_heuristic(self):
        config = _config(RECOMMENDATION_VPA_ENABLED=False, RECOMMENDATION_KARPENTER_ENABLED=False)
        _registry, configured = build_source_registry(config, entry_points=lambda: [])
        enabled = {item.id: item.enabled for item in configured}
        assert enabled["vpa"] == EnabledMode.FALSE
        assert enabled["greenkube.nodepool"] == EnabledMode.FALSE
        assert enabled["greenkube.rightsizing_cpu"] == EnabledMode.AUTO
        ids = [s.source_id for s in build_signal_sources(config, entry_points=lambda: [])]
        assert ids == NATIVE_IDS

    def test_sources_config_overrides_flags_and_disables_one_analyzer(self):
        config = _config(
            RECOMMENDATION_VPA_ENABLED=False,
            SOURCES_CONFIG='{"vpa": {"enabled": "true"}, "greenkube.orphaned_lb": {"enabled": false}}',
        )
        ids = [s.source_id for s in build_signal_sources(config, entry_points=lambda: [])]
        assert "vpa" in ids
        assert "greenkube.orphaned_lb" not in ids

    def test_invalid_settings_disable_only_that_source(self):
        config = _config(SOURCES_CONFIG='{"vpa": {"typo": 1}, "nope": {}}')
        registry, configured = build_source_registry(config, entry_points=lambda: [])
        vpa = next(item for item in configured if item.id == "vpa")
        assert vpa.error is not None and "typo" in vpa.error
        assert [p.code for p in registry.problems] == ["unknown_source"]
        ids = [s.source_id for s in build_signal_sources(config, entry_points=lambda: [])]
        assert "vpa" not in ids and "greenkube.rightsizing_cpu" in ids


class TestPlugins:
    def test_entry_point_sources_are_registered_with_validated_settings(self):
        config = _config(SOURCES_CONFIG='{"acme": {"url": "https://acme.example", "window_days": 14}}')
        registry, configured = build_source_registry(
            config, entry_points=lambda: [_entry_point("acme", lambda: AcmeSource)]
        )
        acme = next(item for item in configured if item.id == "acme")
        assert acme.descriptor.origin == "plugin"
        assert acme.descriptor.distribution == "acme-greenkube-source"
        assert acme.enabled == EnabledMode.AUTO
        assert isinstance(acme.settings, AcmeSettings) and acme.settings.window_days == 14
        sources = build_signal_sources(config, entry_points=lambda: [_entry_point("acme", lambda: AcmeSource)])
        assert sources[-1].source_id == "acme"
        settings = sources[-1].settings
        assert isinstance(settings, AcmeSettings) and settings.url == "https://acme.example"

    def test_a_callable_entry_point_may_register_several_sources(self):
        class Other(AcmeSource):
            id: ClassVar[str] = "acme.other"

        registry, configured = build_source_registry(
            Config(), entry_points=lambda: [_entry_point("bundle", lambda: lambda: [AcmeSource, Other])]
        )
        assert {"acme", "acme.other"} <= {item.id for item in configured}

    def test_broken_squatting_or_duplicate_plugins_are_reported_never_raised(self):
        points = [
            _entry_point("broken", _boom),
            _entry_point("squatter", lambda: Squatter),
            _entry_point("acme", lambda: AcmeSource),
            _entry_point("dup", lambda: Duplicate),
            _entry_point("vpa-clone", lambda: type("Vpa", (AcmeSource,), {"id": "vpa"})),
        ]
        registry, configured = build_source_registry(Config(), entry_points=lambda: points)
        assert [item.id for item in configured][-1] == "acme"
        problems = {(p.entry_point or "").split(" ")[0]: p for p in registry.problems}
        assert set(problems) == {"broken", "squatter", "dup", "vpa-clone"}
        assert all(p.code == "plugin_load_failed" for p in problems.values())
        assert "acme_sdk" in problems["broken"].message
        assert "reserved" in problems["squatter"].message
        assert "already registered" in problems["dup"].message

    def test_plugins_can_be_switched_off(self):
        called = []
        _registry, configured = build_source_registry(
            _config(SOURCES_PLUGINS_ENABLED=False), entry_points=lambda: called.append(1) or []
        )
        assert called == []
        assert "acme" not in {item.id for item in configured}

    def test_constructor_failures_are_isolated(self):
        class Exploding(AcmeSource):
            def __init__(self, settings, deps):
                raise RuntimeError("cannot reach acme")

        sources = build_signal_sources(Config(), entry_points=lambda: [_entry_point("x", lambda: Exploding)])
        assert "acme" not in [s.source_id for s in sources]
        assert "greenkube.rightsizing_cpu" in [s.source_id for s in sources]


class TestSourcesConfigSetting:
    def test_parse_normalizes_booleans_and_rejects_non_objects(self):
        assert parse_sources_config('{"vpa": {"enabled": false}, "x": null}') == {"vpa": {"enabled": "false"}, "x": {}}
        assert parse_sources_config("") == {}
        with pytest.raises(ValueError):
            parse_sources_config("[1, 2]")
        with pytest.raises(ValueError):
            parse_sources_config('{"vpa": "on"}')

    def test_config_rejects_invalid_json_at_startup(self, monkeypatch):
        monkeypatch.setenv("SOURCES_CONFIG", "{not json")
        with pytest.raises(ValidationError):
            Config()

    def test_defaults(self):
        config = Config()
        assert config.SOURCES_PLUGINS_ENABLED is True
        assert config.SOURCES_CONFIG == ""
