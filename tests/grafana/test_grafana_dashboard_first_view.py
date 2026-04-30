import json
from pathlib import Path

DASHBOARD_PATH = Path(__file__).resolve().parents[2] / "dashboards" / "greenkube-grafana.json"


def _load_dashboard() -> dict:
    with DASHBOARD_PATH.open() as dashboard_file:
        return json.load(dashboard_file)


def _top_level_panel(title: str) -> dict:
    dashboard = _load_dashboard()
    return next(panel for panel in dashboard["panels"] if panel.get("title") == title)


def _panel(title: str) -> dict:
    dashboard = _load_dashboard()
    return next(panel for panel in dashboard["panels"] if panel.get("title") == title)


def _panel_by_id(panel_id: int) -> dict:
    dashboard = _load_dashboard()
    return next(panel for panel in dashboard["panels"] if panel.get("id") == panel_id)


def test_command_center_is_the_first_visible_dashboard_section():
    dashboard = _load_dashboard()
    first_panel = dashboard["panels"][0]

    assert first_panel["type"] == "row"
    assert first_panel["title"] == "GreenKube Impact Command Center"
    assert first_panel["collapsed"] is False
    assert first_panel["gridPos"]["y"] == 0


def test_datasource_and_namespace_variables_resolve_live_prometheus():
    dashboard = _load_dashboard()
    variables = {variable["name"]: variable for variable in dashboard["templating"]["list"]}

    assert variables["DS_PROMETHEUS"]["current"] == {
        "text": "prometheus",
        "value": "P1809F7CD0C75ACF3",
    }
    assert 'cluster=~"$cluster"' in variables["namespace"]["query"]
    assert variables["dashboard_window"]["current"] == {"text": "7d", "value": "604800s"}
    assert "YTD : ytd" in variables["dashboard_window"]["query"]


def test_first_view_promotes_greenkube_value_signals():
    dashboard = _load_dashboard()
    first_view_titles = [
        panel.get("title", "") for panel in dashboard["panels"] if panel.get("gridPos", {}).get("y", 999) <= 13
    ]

    assert "Sustainability Score Radar" in first_view_titles
    assert "Footprint & Cost Mix" in first_view_titles
    assert "GreenKube Impact" in first_view_titles
    assert "Action Priorities" in first_view_titles


def test_top_left_panel_is_sustainability_radar_chart():
    panel = _panel("Sustainability Score Radar")
    target_exprs = [target.get("expr", "") for target in panel.get("targets", [])]
    get_option = panel["options"]["getOption"]

    assert panel["type"] == "volkovlabs-echarts-panel"
    assert panel["gridPos"] == {"x": 0, "y": 1, "w": 8, "h": 12}
    assert panel["datasource"]["uid"] == "${DS_PROMETHEUS}"
    assert any("greenkube_sustainability_dimension_score" in expr for expr in target_exprs)
    assert all('namespace=~"$namespace"' in expr for expr in target_exprs)
    assert all('namespace!="__all__"' in expr for expr in target_exprs)
    assert panel["options"]["renderer"] == "canvas"
    assert panel["options"]["editorMode"] == "code"
    assert panel["options"]["map"] == "none"
    assert "type: 'radar'" in get_option
    assert "lineStyle: { color: palette.cyan, width: 3 }" in get_option
    assert "areaStyle: { color: 'rgba(0, 255, 212, 0.30)' }" in get_option
    assert "trigger: 'item'" in get_option
    assert "legend: { show: false }" in get_option


def test_sustainability_radar_displays_all_dimension_values():
    panel = _panel("Sustainability Score Radar")
    target_labels = {target["legendFormat"] for target in panel["targets"]}
    get_option = panel["options"]["getOption"]

    assert len(panel["targets"]) == 8
    assert target_labels == {
        "Resource efficiency",
        "Carbon efficiency",
        "Waste elimination",
        "Node efficiency",
        "Scaling practices",
        "Carbon aware",
        "Stability",
        "Global score",
    }
    assert "Resource efficiency" in get_option
    assert "Carbon efficiency" in get_option
    assert "Waste elimination" in get_option
    assert "Node efficiency" in get_option
    assert "Scaling practices" in get_option
    assert "Carbon aware" in get_option
    assert "Stability" in get_option


def test_radar_renders_global_score_inside_echarts():
    panel = _panel("Sustainability Score Radar")
    get_option = panel["options"]["getOption"]
    global_target = next(t for t in panel["targets"] if t["refId"] == "H")

    # Score value is fetched from refId H
    assert "greenkube_sustainability_score" in global_target["expr"]
    assert global_target["legendFormat"] == "Global score"

    # Score is rendered as ECharts graphic text directly inside the chart
    assert "valueFor('H', true)" in get_option
    assert "scoreColor" in get_option
    assert "Math.round(globalScore)" in get_option
    assert "/ 100" in get_option
    assert "shape: { r: 58 }" not in get_option
    assert "type: 'circle'" not in get_option
    assert "zlevel: 1000" in get_option
    assert "z: 100000" in get_option
    assert "top: 'middle'" in get_option
    assert "top: '55%'" in get_option
    assert "textVerticalAlign: 'middle'" in get_option
    assert "textVerticalAlign: 'top'" in get_option
    assert "textBorderWidth" not in get_option
    assert "textBorderColor" not in get_option
    assert "seriesForRef(refId)" in get_option
    assert "series?.refId === refId" in get_option
    assert "meta?.custom?.refId" in get_option
    # both score number and /100 share the same color variable
    assert get_option.count("fill: scoreTextColor") == 2
    assert "label: { show: false }" in get_option
    assert "globalScore >= 80 ? palette.cyan" in get_option
    assert "globalScore >= 60 ? palette.amber" in get_option
    assert "palette.red" in get_option


def test_command_center_groups_footprint_and_cost_in_echarts():
    panel = _panel("Footprint & Cost Mix")
    target_labels = {target["legendFormat"] for target in panel["targets"]}
    get_option = panel["options"]["getOption"]

    assert panel["type"] == "volkovlabs-echarts-panel"
    assert panel["gridPos"] == {"x": 8, "y": 1, "w": 8, "h": 6}
    assert target_labels == {"Scope 2", "Scope 3", "Cloud cost"}
    assert "greenkube_dashboard_summary_co2e_grams_total" in panel["targets"][0]["expr"]
    assert "greenkube_dashboard_summary_co2e_grams_total" in panel["targets"][1]["expr"]
    assert "greenkube_dashboard_summary_cost_dollars_total" in panel["targets"][2]["expr"]
    assert 'window="$dashboard_window"' in panel["targets"][0]["expr"]
    assert 'namespace=~"$namespace"' in panel["targets"][0]["expr"]
    assert 'namespace!="__all__"' in panel["targets"][0]["expr"]
    assert 'scope="scope2"' in panel["targets"][0]["expr"]
    assert 'scope="scope3"' in panel["targets"][1]["expr"]
    assert 'scope="scope2"}}' not in panel["targets"][0]["expr"]
    assert 'scope="scope3"}}' not in panel["targets"][1]["expr"]
    assert 'namespace="__all__"}}' not in panel["targets"][2]["expr"]
    assert "greenkube_cluster_co2e_grams_total" in panel["targets"][0]["expr"]
    assert "greenkube_cluster_embodied_co2e_grams_total" in panel["targets"][1]["expr"]
    assert "greenkube_cluster_cost_dollars_total" in panel["targets"][2]["expr"]
    assert "type: 'bar'" in get_option
    assert "palette.cyan" in get_option
    assert "palette.amber" in get_option
    assert "<$0.01" in get_option
    assert "value.toFixed(3)" in get_option
    assert "absolute >= 1 ? 2 : 3" in get_option


def test_command_center_groups_impact_metrics_in_echarts():
    panel = _panel("GreenKube Impact")
    target_labels = {target["legendFormat"] for target in panel["targets"]}

    assert panel["type"] == "volkovlabs-echarts-panel"
    assert panel["gridPos"] == {"x": 16, "y": 1, "w": 8, "h": 6}
    assert target_labels == {"CO₂e avoided", "Cost avoided", "Implemented", "Measured coverage"}
    assert "greenkube_dashboard_savings_co2e_grams_total" in panel["targets"][0]["expr"]
    assert "greenkube_dashboard_savings_cost_dollars_total" in panel["targets"][1]["expr"]
    assert 'window="$dashboard_window"' in panel["targets"][0]["expr"]
    assert 'recommendation_type="all"' in panel["targets"][0]["expr"]
    assert 'namespace=~"$namespace"' in panel["targets"][0]["expr"]
    assert 'namespace!="__all__"' in panel["targets"][0]["expr"]
    assert "increase(" not in panel["targets"][0]["expr"]
    assert "increase(" not in panel["targets"][1]["expr"]
    assert "greenkube_recommendations_implemented_total" in panel["targets"][2]["expr"]
    assert 'namespace=~"$namespace"' in panel["targets"][2]["expr"]
    assert "greenkube_estimated_metrics_ratio" in panel["targets"][3]["expr"]
    assert 'namespace=~"$namespace"' in panel["targets"][3]["expr"]


def test_top_three_action_priorities_are_grouped_in_one_echarts_panel():
    panel = _top_level_panel("Action Priorities")
    target_labels = {target["legendFormat"] for target in panel["targets"]}
    get_option = panel["options"]["getOption"]

    assert panel["type"] == "volkovlabs-echarts-panel"
    assert panel["gridPos"] == {"x": 8, "y": 7, "w": 16, "h": 6}
    assert target_labels == {"{{namespace}}", "{{type}}"}
    assert all(target["instant"] is True for target in panel["targets"])
    assert "greenkube_dashboard_summary_co2e_grams_total" in panel["targets"][0]["expr"]
    assert "greenkube_dashboard_summary_cost_dollars_total" in panel["targets"][1]["expr"]
    assert 'window="$dashboard_window"' in panel["targets"][0]["expr"]
    assert 'namespace=~"$namespace"' in panel["targets"][0]["expr"]
    assert 'namespace!="__all__"' in panel["targets"][0]["expr"]
    assert 'scope="all"' in panel["targets"][0]["expr"]
    assert 'namespace=~"$namespace"' in panel["targets"][2]["expr"]
    assert 'namespace!="__all__"' in panel["targets"][2]["expr"]
    assert ".filter((series) => matchesRefId(series, refId))" in get_option
    assert "pointsFor(group.refId, group.labelKey, 3)" in get_option
    assert "CO₂e namespaces" in get_option
    assert "Cost namespaces" in get_option
    assert "Recommendation types" in get_option
    assert "<$0.01" in get_option
    assert "value.toFixed(3)" in get_option


def test_dashboard_has_native_node_region_geomap():
    panel = _panel("Node Region Cleanliness Map")
    target_exprs = [target.get("expr", "") for target in panel.get("targets", [])]

    assert panel["type"] == "geomap"
    assert any("greenkube_zone_grid_intensity_gco2_kwh" in expr for expr in target_exprs)
    assert any("node_count" in expr for expr in target_exprs)
    assert any("bubble_size" in expr for expr in target_exprs)
    assert any("bubble_label" in expr for expr in target_exprs)
    # PUE join removed — grid intensity remains the map value
    assert all("greenkube_pue" not in expr for expr in target_exprs)
    assert len(panel["targets"]) == 1
    assert panel["options"]["layers"][0]["location"]["gazetteer"] == "/public/build/gazetteer/countries.json"
    assert panel["options"]["layers"][0]["config"]["style"]["text"]["field"] == "bubble_label"
    assert panel["options"]["layers"][0]["config"]["style"]["size"]["field"] == "bubble_size"
    assert panel["options"]["layers"][0]["config"]["style"]["color"]["field"] == "Value"
    assert panel["transformations"][0]["id"] == "convertFieldType"
    conversions = panel["transformations"][0]["options"]["conversions"]
    assert {"targetField": "node_count", "destinationType": "number"} in conversions
    assert {"targetField": "bubble_size", "destinationType": "number"} in conversions
    assert panel["options"]["layers"][0]["config"]["style"]["symbol"]["fixed"].endswith("circle.svg")
    assert panel["fieldConfig"]["defaults"]["thresholds"]["steps"][-1]["color"] == "red"
