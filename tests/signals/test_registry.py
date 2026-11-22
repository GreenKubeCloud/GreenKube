"""Source registry: built-ins, entry-point plugins and SOURCES_CONFIG (roadmap E5)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar, FrozenSet, Optional

import pytest
from pydantic import ValidationError

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.optimization.registry import build_signal_sources, build_source_registry
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.signals.model import Capability
from greenkube.signals.registry import parse_sources_config
from greenkube.signals.sdk import EnabledMode, Source, SourceBatch, SourceSettings, SourceTier

NATIVE_IDS = [
    "greenkube.rightsizing_cpu",
    "greenkube.rightsizing_memory",
    "greenkube.orphaned_pv",
    "greenkube.orphaned_lb",
]
#: Signal-only connectors (roadmap G), after the native analyzers.
CONNECTOR_IDS = ["krr", "push"]


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
        assert [item.id for item in configured] == ["vpa", *NATIVE_IDS, *CONNECTOR_IDS]
        assert all(item.descriptor.origin == "builtin" for item in configured)
        assert registry.problems == []

    def test_engine_runs_the_sdk_sources_by_default(self):
        engine = OptimizationEngine(config=Config(), sources=build_signal_sources(Config(), entry_points=lambda: []))
        assert [source.source_id for source in engine.sources] == ["vpa", *NATIVE_IDS, *CONNECTOR_IDS]
        # Run records count vpa, greenkube and each signal-only connector.
        assert engine.source_group_count == 2 + len(CONNECTOR_IDS)

    def test_legacy_flag_disables_vpa(self):
        config = _config(RECOMMENDATION_VPA_ENABLED=False)
        _registry, configured = build_source_registry(config, entry_points=lambda: [])
        enabled = {item.id: item.enabled for item in configured}
        assert enabled["vpa"] == EnabledMode.FALSE
        assert enabled["greenkube.rightsizing_cpu"] == EnabledMode.AUTO
        ids = [s.source_id for s in build_signal_sources(config, entry_points=lambda: [])]
        assert ids == [*NATIVE_IDS, *CONNECTOR_IDS]

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

    def test_settings_of_removed_connectors_are_ignored_without_a_problem(self):
        """B.3: SOURCES_CONFIG of kubecost, polaris, gke and azure_advisor is accepted and ignored."""
        config = _config(SOURCES_CONFIG='{"kubecost": {"url": "http://kubecost"}, "gke": {}, "nope": {}}')
        registry, configured = build_source_registry(config, entry_points=lambda: [])
        assert [(p.code, p.source_id) for p in registry.problems] == [("unknown_source", "nope")]
        assert not {"kubecost", "polaris", "gke", "azure_advisor"} & {item.id for item in configured}

    def test_settings_of_removed_analyzers_are_ignored_without_a_problem(self):
        """B.1: SOURCES_CONFIG of the removed advisory analyzers and NodePool heuristic is accepted and ignored."""
        config = _config(SOURCES_CONFIG='{"greenkube.carbon_aware": {"enabled": false}, "greenkube.nodepool": {}}')
        registry, configured = build_source_registry(config, entry_points=lambda: [])
        assert registry.problems == []
        assert not {"greenkube.carbon_aware", "greenkube.nodepool"} & {item.id for item in configured}


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


class _CleanupPlugin(Source):
    """A plugin that tries to push v1 deletion advice around signals and consensus."""

    id: ClassVar[str] = "acme.cleanup"
    display_name: ClassVar[str] = "Acme cleanup"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CLEANUP})

    async def collect(self, ctx):
        return SourceBatch(
            recommendations=[
                Recommendation(
                    pod_name="cilium-gateway",
                    namespace="kube-system",
                    type=RecommendationType.ORPHANED_LOAD_BALANCER,
                    scope="workload",
                    description="delete it",
                    potential_savings_cost=10.0,
                )
            ]
        )


class TestPluginTrust:
    """FAIL CLOSED: third-party sources never feed the v1 pipeline directly."""

    def test_a_plugin_declaring_a_legacy_source_is_rejected(self):
        class Impostor(AcmeSource):
            id: ClassVar[str] = "acme.impostor"
            legacy_source: ClassVar[Optional[str]] = "greenkube"

        registry, configured = build_source_registry(
            Config(), entry_points=lambda: [_entry_point("impostor", lambda: Impostor)]
        )
        assert "acme.impostor" not in {item.id for item in configured}
        [problem] = registry.problems
        assert problem.code == "plugin_load_failed"
        assert "legacy_source" in problem.message

    def test_a_plugin_subclassing_a_builtin_adapter_is_rejected(self):
        from greenkube.core.optimization.analyzers import DEFAULT_ANALYZERS
        from greenkube.signals.adapters import analyzer_source_class

        base = analyzer_source_class(DEFAULT_ANALYZERS[0])
        fake = type("FakeNative", (base,), {"id": "acme.native", "legacy_source": None})
        registry, configured = build_source_registry(
            Config(), entry_points=lambda: [_entry_point("fake", lambda: fake)]
        )
        assert "acme.native" not in {item.id for item in configured}
        assert "built-in adapter" in registry.problems[0].message

    def test_instances_carry_their_registry_origin(self):
        sources = build_signal_sources(Config(), entry_points=lambda: [_entry_point("acme", lambda: AcmeSource)])
        origins = {source.source_id: source.registry_origin for source in sources}
        assert origins["acme"] == "plugin"
        assert origins["vpa"] == "builtin" and origins["greenkube.orphaned_lb"] == "builtin"

    async def test_plugin_recommendations_never_reach_the_engine_output(self):
        sources = [
            source
            for source in build_signal_sources(
                Config(), entry_points=lambda: [_entry_point("cleanup", lambda: _CleanupPlugin)]
            )
            if source.source_id == "acme.cleanup"
        ]
        engine = OptimizationEngine(config=Config(), sources=sources)
        result = await engine.collect(OptimizationContext(config=Config(), metrics=[]))
        assert result.recommendations == []
        [report] = result.source_reports
        assert report.recommendations == [] and report.legacy_source is None
        assert report.run.recommendation_count == 0
        [diagnostic] = [d for d in report.run.diagnostics if d.code == "recommendations_ignored"]
        assert diagnostic.count == 1
        # The plugin forms its own v1 outcome: it never joins (or completes) the built-in 'greenkube' group.
        assert [outcome.source for outcome in result.source_outcomes] == ["acme.cleanup"]


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
