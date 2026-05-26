import json
import os

import pytest

DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "dashboards", "greenkube-grafana.json"
)


class TestGrafanaDashboardFile:
    """Tests for the Grafana dashboard JSON file structure and completeness."""

    def test_grafana_dashboard_exists(self):
        assert os.path.exists(DASHBOARD_PATH), f"Dashboard file not found: {DASHBOARD_PATH}"

    def test_grafana_dashboard_valid_json(self):
        with open(DASHBOARD_PATH, "r") as f:
            try:
                dashboard = json.load(f)
            except json.JSONDecodeError as e:
                pytest.fail(f"Dashboard JSON invalid: {e}")
        assert isinstance(dashboard, dict)

    def test_dashboard_has_required_top_level_keys(self):
        dashboard = _load_dashboard()
        for key in ("title", "panels", "templating", "time", "refresh"):
            assert key in dashboard, f"Dashboard missing top-level key '{key}'"

    def test_dashboard_uses_prometheus_datasource_variable(self):
        """All panels must reference the templated datasource variable, not a hardcoded name."""
        dashboard = _load_dashboard()
        for panel in _all_panels(dashboard):
            ds = panel.get("datasource", {})
            if isinstance(ds, dict) and ds.get("type") == "prometheus":
                uid = ds.get("uid", "")
                assert uid == "${DS_PROMETHEUS}", (
                    f"Panel '{panel.get('title')}' uses hardcoded datasource uid '{uid}' "
                    "instead of '${DS_PROMETHEUS}'"
                )

    def test_dashboard_has_namespace_template_variable(self):
        dashboard = _load_dashboard()
        var_names = [v["name"] for v in dashboard.get("templating", {}).get("list", [])]
        assert "namespace" in var_names, "Dashboard missing 'namespace' template variable"

    def test_dashboard_has_datasource_template_variable(self):
        dashboard = _load_dashboard()
        var_names = [v["name"] for v in dashboard.get("templating", {}).get("list", [])]
        assert "DS_PROMETHEUS" in var_names, "Dashboard missing 'DS_PROMETHEUS' template variable"

    def test_dashboard_has_carbon_panels(self):
        titles = _all_panel_titles()
        assert any("CO₂" in t or "CO2" in t or "Carbon" in t for t in titles), "No CO2/Carbon panel found"

    def test_dashboard_has_cost_panels(self):
        titles = _all_panel_titles()
        assert any("Cost" in t for t in titles), "No Cost panel found"

    def test_dashboard_keeps_requested_panel_set(self):
        titles = _all_panel_titles()
        assert titles == [
            "Sustainability Score Radar",
            "Footprint & Cost Mix",
            "GreenKube Impact",
            "Action Priorities",
            "Top Actionable Recommendations",
            "CO₂e by Namespace",
            "Cost by Namespace",
            "Node Region Cleanliness Map",
            "Top 15 Pods — CO₂e",
            "Top 15 Pods — Cost",
        ]

    def test_command_center_retains_recommendation_metrics(self):
        dashboard = _load_dashboard()
        assert any(
            "greenkube_recommendations" in target.get("expr", "")
            for panel in _all_panels(dashboard)
            for target in panel.get("targets", [])
        )

    def test_dashboard_has_node_panels(self):
        titles = _all_panel_titles()
        assert any("Node" in t for t in titles), "No Node panel found"

    def test_dashboard_has_grid_intensity_panels(self):
        dashboard = _load_dashboard()
        assert any(
            "greenkube_zone_grid_intensity_gco2_kwh" in target.get("expr", "")
            for panel in _all_panels(dashboard)
            for target in panel.get("targets", [])
        ), "No Grid Intensity panel found"

    def test_dashboard_panel_count_matches_reduced_scope(self):
        """The dashboard should contain only the requested non-row panels."""
        dashboard = _load_dashboard()
        panels = _all_panels(dashboard)
        assert len(panels) == 10

    def test_all_panels_have_targets(self):
        """Every non-row panel must have at least one target (query)."""
        dashboard = _load_dashboard()
        for panel in _all_panels(dashboard):
            if panel.get("type") == "row":
                continue
            targets = panel.get("targets", [])
            assert len(targets) > 0, f"Panel '{panel.get('title')}' has no targets"

    def test_panels_use_greenkube_metrics(self):
        """All PromQL expressions should reference greenkube_ prefixed metrics."""
        dashboard = _load_dashboard()
        for panel in _all_panels(dashboard):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                assert "greenkube_" in expr, (
                    f"Panel '{panel.get('title')}' target expr '{expr}' does not reference any greenkube_ metric"
                )

    def test_dashboard_has_rows_for_organization(self):
        """Dashboard should use only the requested section rows, in order."""
        dashboard = _load_dashboard()
        row_panels = [p for p in dashboard.get("panels", []) if p.get("type") == "row"]
        assert [row.get("title") for row in row_panels] == [
            "GreenKube Impact Command Center",
            "Actionable Recommendations",
            "CO₂e and Cost by Namespace",
            "Regional Node Cleanliness",
            "Top Emitters & Spenders",
        ]

    def test_dashboard_input_requires_prometheus_datasource(self):
        """Dashboard __inputs should declare a Prometheus datasource for import."""
        dashboard = _load_dashboard()
        inputs = dashboard.get("__inputs", [])
        ds_inputs = [i for i in inputs if i.get("type") == "datasource"]
        assert len(ds_inputs) >= 1, "Dashboard __inputs missing datasource declaration"
        assert any(i.get("pluginId") == "prometheus" for i in ds_inputs), "No Prometheus datasource input declared"

    def test_namespace_variable_defaults_to_all(self):
        """Namespace variable must default to 'All' so all namespaces are visible on first load."""
        dashboard = _load_dashboard()
        ns_var = _find_template_variable(dashboard, "namespace")
        assert ns_var is not None, "Namespace template variable not found"
        assert ns_var.get("includeAll") is True, "Namespace variable must have includeAll=true"
        assert ns_var.get("multi") is True, "Namespace variable must have multi=true"
        current = ns_var.get("current", {})
        assert current.get("text") == "All", f"Namespace variable default should be 'All', got '{current.get('text')}'"
        assert current.get("value") == "$__all", (
            f"Namespace variable default value should be '$__all', got '{current.get('value')}'"
        )

    def test_unused_variables_are_removed(self):
        """Node and region variables are no longer used by the reduced dashboard."""
        dashboard = _load_dashboard()
        var_names = [v["name"] for v in dashboard.get("templating", {}).get("list", [])]
        assert "node" not in var_names
        assert "region" not in var_names


def _load_dashboard() -> dict:
    with open(DASHBOARD_PATH, "r") as f:
        return json.load(f)


def _find_template_variable(dashboard: dict, name: str) -> dict | None:
    """Find a template variable by name."""
    for var in dashboard.get("templating", {}).get("list", []):
        if var.get("name") == name:
            return var
    return None


def _all_panels(dashboard: dict) -> list:
    """Recursively collect all panels including those nested inside rows."""
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
