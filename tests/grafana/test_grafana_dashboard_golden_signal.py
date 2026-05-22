# tests/test_grafana_dashboard_golden_signal.py
"""Tests for the reduced Grafana dashboard scope and shared cluster variable."""

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


class TestReducedDashboardVariables:
    """Dashboard should keep only variables used by the reduced panel set."""

    def test_unused_node_and_region_variables_are_removed(self):
        dashboard = _load_dashboard()
        variable_names = {variable["name"] for variable in dashboard.get("templating", {}).get("list", [])}

        assert variable_names == {"DS_PROMETHEUS", "cluster", "namespace", "dashboard_window"}


class TestReducedGrafanaDashboardScope:
    """Dashboard must contain the requested sections and omit deleted ones."""

    def test_kept_rows_match_requested_scope(self):
        dashboard = _load_dashboard()
        rows = [p for p in dashboard.get("panels", []) if p.get("type") == "row"]

        assert [row.get("title") for row in rows] == [
            "GreenKube Impact Command Center",
            "CO₂e and Cost by Namespace",
            "Regional Node Cleanliness",
            "Top Emitters & Spenders",
        ]

    def test_removed_detail_rows_are_absent(self):
        dashboard = _load_dashboard()
        row_titles = {p.get("title", "") for p in dashboard.get("panels", []) if p.get("type") == "row"}

        assert not row_titles.intersection(
            {
                "Carbon, Cost & Energy Trends",
                "Sustainability Score Breakdown",
                "Resource Efficiency",
                "Node Analysis",
                "Network & Storage I/O",
                "Pod Stability",
                "Recommendations & Savings",
                "GreenKube Self-Monitoring",
            }
        )

    def test_removed_detail_panels_are_absent(self):
        titles = set(_all_panel_titles())

        assert not titles.intersection(
            {
                "Energy by Namespace (kWh)",
                "Namespace Summary",
                "Top 15 Pods — Energy",
                "Top 15 Pods — Embodied CO₂e (Scope 3)",
                "Carbon Intensity Score (avg)",
            }
        )

    def test_sustainability_score_panel_exists(self):
        """A panel showing the overall sustainability score must exist."""
        titles = _all_panel_titles()
        assert any("Sustainability Score" in t or "Sustainability" in t for t in titles), (
            f"No Sustainability Score panel found. Titles: {titles}"
        )


class TestDashboardPanelsUseCluster:
    """Key panels should filter by the shared $cluster template variable."""

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
