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
        assert any("CO2" in t or "Carbon" in t for t in titles), "No CO2/Carbon panel found"

    def test_dashboard_has_cost_panels(self):
        titles = _all_panel_titles()
        assert any("Cost" in t for t in titles), "No Cost panel found"

    def test_dashboard_has_energy_panels(self):
        titles = _all_panel_titles()
        assert any("Energy" in t or "Joules" in t or "kWh" in t for t in titles), "No Energy panel found"

    def test_dashboard_has_recommendation_panels(self):
        titles = _all_panel_titles()
        assert any("Recommendation" in t or "Savings" in t for t in titles), "No Recommendation panel found"

    def test_dashboard_has_cpu_panels(self):
        titles = _all_panel_titles()
        assert any("CPU" in t for t in titles), "No CPU panel found"

    def test_dashboard_has_memory_panels(self):
        titles = _all_panel_titles()
        assert any("Memory" in t for t in titles), "No Memory panel found"

    def test_dashboard_has_node_panels(self):
        titles = _all_panel_titles()
        assert any("Node" in t for t in titles), "No Node panel found"

    def test_dashboard_has_network_panels(self):
        titles = _all_panel_titles()
        assert any("Network" in t for t in titles), "No Network panel found"

    def test_dashboard_has_grid_intensity_panels(self):
        titles = _all_panel_titles()
        assert any("Grid" in t or "Intensity" in t for t in titles), "No Grid Intensity panel found"

    def test_dashboard_minimum_panel_count(self):
        """A comprehensive FinGreenOps dashboard should have at least 15 panels."""
        dashboard = _load_dashboard()
        panels = _all_panels(dashboard)
        assert len(panels) >= 15, f"Dashboard has only {len(panels)} panels, expected >= 15"

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
        """Dashboard should use row panels to organize sections."""
        dashboard = _load_dashboard()
        row_panels = [p for p in dashboard.get("panels", []) if p.get("type") == "row"]
        assert len(row_panels) >= 4, f"Dashboard has only {len(row_panels)} rows, expected >= 4"

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

    def test_node_variable_defaults_to_all(self):
        """Node variable must default to 'All' so all nodes are visible on first load."""
        dashboard = _load_dashboard()
        node_var = _find_template_variable(dashboard, "node")
        assert node_var is not None, "Node template variable not found"
        assert node_var.get("includeAll") is True, "Node variable must have includeAll=true"
        current = node_var.get("current", {})
        assert current.get("text") == "All", f"Node variable default should be 'All', got '{current.get('text')}'"
        assert current.get("value") == "$__all", (
            f"Node variable default value should be '$__all', got '{current.get('value')}'"
        )


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
