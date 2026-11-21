# tests/core/optimization/test_registry.py
"""Tests for recommendation source selection."""

from greenkube.core.config import Config
from greenkube.core.optimization.registry import build_sources


class TestBuildSources:
    def test_all_sources_enabled_by_default(self):
        # Phase 6: VPA and Karpenter are enabled by default and detect their
        # CRDs at runtime, so an absent CRD disables the source gracefully.
        sources = build_sources(Config())
        assert [s.name for s in sources] == ["vpa", "karpenter", "greenkube"]

    def test_native_only_when_sources_disabled(self):
        cfg = Config()
        cfg.RECOMMENDATION_VPA_ENABLED = False
        cfg.RECOMMENDATION_KARPENTER_ENABLED = False
        sources = build_sources(cfg)
        assert [s.name for s in sources] == ["greenkube"]

    def test_vpa_enabled(self):
        cfg = Config()
        cfg.RECOMMENDATION_KARPENTER_ENABLED = False
        sources = build_sources(cfg)
        names = [s.name for s in sources]
        assert names == ["vpa", "greenkube"]

    def test_karpenter_enabled(self):
        cfg = Config()
        cfg.RECOMMENDATION_VPA_ENABLED = False
        sources = build_sources(cfg)
        assert [s.name for s in sources] == ["karpenter", "greenkube"]

    def test_all_sources_enabled(self):
        cfg = Config()
        cfg.RECOMMENDATION_VPA_ENABLED = True
        cfg.RECOMMENDATION_KARPENTER_ENABLED = True
        assert [s.name for s in build_sources(cfg)] == ["vpa", "karpenter", "greenkube"]
