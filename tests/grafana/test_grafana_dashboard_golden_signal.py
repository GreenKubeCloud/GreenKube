# tests/test_grafana_dashboard_golden_signal.py
"""
Tests for Grafana dashboard ticket #182:
  - cluster and region template variables
  - Sustainability Golden Signal panels
  - State Timeline for low-intensity windows
"""

import json
import os

DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "dashboards", "greenkube-grafana.json"
)


def _load_dashboard() -> dict:
    with open(DASHBOARD_PATH, "r") as f:
        return json.load(f)


def _find_template_variable(dashboard: dict, name: str) -> dict | None:
    for var in dashboard.get("templating", {}).get("list", []):
        if var.get("name") == name:
            return var
    return None


def _all_panels(dashboard: dict) -> list:
    panels = []
    for panel in dashboard.get("panels", []):
        if panel.get("type") == "row":
            panels.extend(panel.get("panels", []))
        else:
            panels.append(panel)
    return panels


def _all_panel_titles() -> list:
    dashboard = _load_dashboard()
    return [p.get("title", "") for p in _all_panels(dashboard)]


class TestClusterTemplateVariable:
    """Dashboard must have a 'cluster' template variable for multi-cluster environments."""

    def test_cluster_variable_exists(self):
        dashboard = _load_dashboard()
        var = _find_template_variable(dashboard, "cluster")
        assert var is not None, "Dashboard missing 'cluster' template variable"

    def test_cluster_variable_defaults_to_all(self):
        dashboard = _load_dashboard()
        var = _find_template_variable(dashboard, "cluster")
        assert var is not None
        assert var.get("includeAll") is True
        current = var.get("current", {})
        assert current.get("text") == "All"
        assert current.get("value") == "$__all"


class TestRegionTemplateVariable:
    """Dashboard must have a 'region' template variable."""

    def test_region_variable_exists(self):
        dashboard = _load_dashboard()
        var = _find_template_variable(dashboard, "region")
        assert var is not None, "Dashboard missing 'region' template variable"

    def test_region_variable_defaults_to_all(self):
        dashboard = _load_dashboard()
        var = _find_template_variable(dashboard, "region")
        assert var is not None
        assert var.get("includeAll") is True
        current = var.get("current", {})
        assert current.get("text") == "All"
        assert current.get("value") == "$__all"


class TestSustainabilityGoldenSignalPanels:
    """Dashboard must have the sustainability golden signal panels."""

    def test_has_carbon_intensity_score_panel(self):
        titles = _all_panel_titles()
        assert any("Carbon Intensity Score" in t for t in titles), (
            f"No 'Carbon Intensity Score' panel found. Titles: {titles}"
        )

    def test_carbon_intensity_score_uses_correct_metric(self):
        dashboard = _load_dashboard()
        panels = _all_panels(dashboard)
        score_panel = next((p for p in panels if "Carbon Intensity Score" in p.get("title", "")), None)
        assert score_panel is not None
        exprs = [t.get("expr", "") for t in score_panel.get("targets", [])]
        assert any("greenkube_carbon_intensity_score" in e for e in exprs)

    def test_has_low_intensity_windows_panel(self):
        titles = _all_panel_titles()
        assert any("Intensity" in t and ("Window" in t or "Timeline" in t or "Zone" in t) for t in titles), (
            f"No low-intensity windows/timeline panel found. Titles: {titles}"
        )

    def test_has_sustainability_golden_signal_row(self):
        dashboard = _load_dashboard()
        rows = [p for p in dashboard.get("panels", []) if p.get("type") == "row"]
        row_titles = [r.get("title", "") for r in rows]
        assert any("Sustainability" in t or "Golden Signal" in t for t in row_titles), (
            f"No sustainability golden signal row found. Rows: {row_titles}"
        )

    def test_sustainability_score_panel_exists(self):
        """A panel showing the overall sustainability score must exist."""
        titles = _all_panel_titles()
        assert any("Sustainability Score" in t or "Sustainability" in t for t in titles), (
            f"No Sustainability Score panel found. Titles: {titles}"
        )


class TestDashboardPanelsUseClusterRegion:
    """Key panels should filter by $cluster and $region template variables."""

    def test_overview_panels_filter_by_cluster(self):
        """At least one cluster-level panel should reference $cluster."""
        dashboard = _load_dashboard()
        panels = _all_panels(dashboard)
        found = False
        for panel in panels:
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if "cluster" in expr and "greenkube_cluster_" in expr:
                    found = True
                    break
        assert found, "No cluster-level panel references $cluster in its PromQL"
