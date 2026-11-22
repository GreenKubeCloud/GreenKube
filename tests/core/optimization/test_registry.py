# tests/core/optimization/test_registry.py
"""Tests for recommendation source selection."""

from greenkube.core.config import Config
from greenkube.core.optimization.registry import build_sources


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
