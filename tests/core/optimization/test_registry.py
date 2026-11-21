# tests/core/optimization/test_registry.py
"""Tests for recommendation source selection and priority parsing."""

from greenkube.core.config import Config
from greenkube.core.optimization.registry import build_sources, parse_source_priority


class TestParseSourcePriority:
    def test_default_order(self):
        priorities = parse_source_priority(None)
        assert priorities["vpa"] > priorities["greenkube"]
        assert "karpenter" not in priorities

    def test_custom_order(self):
        priorities = parse_source_priority("greenkube,vpa")
        assert priorities["greenkube"] > priorities["vpa"]

    def test_empty_string_falls_back_to_default(self):
        assert parse_source_priority("  ") == parse_source_priority(None)


class TestBuildSources:
    def test_vpa_and_native_by_default(self):
        # VPA is enabled by default and detects its CRD at runtime, so an absent CRD
        # disables the source gracefully.
        sources = build_sources(Config())
        assert [s.name for s in sources] == ["vpa", "greenkube"]
        by_name = {s.name: s for s in sources}
        assert by_name["vpa"].priority > by_name["greenkube"].priority

    def test_native_only_when_vpa_disabled(self):
        cfg = Config()
        cfg.RECOMMENDATION_VPA_ENABLED = False
        sources = build_sources(cfg)
        assert [s.name for s in sources] == ["greenkube"]
        assert sources[0].priority == 1

    def test_the_removed_karpenter_flag_changes_nothing(self):
        cfg = Config(RECOMMENDATION_KARPENTER_ENABLED=True)
        assert [s.name for s in build_sources(cfg)] == ["vpa", "greenkube"]
