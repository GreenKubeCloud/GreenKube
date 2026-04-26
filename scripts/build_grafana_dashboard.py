#!/usr/bin/env python3
"""
Rebuilds dashboards/greenkube-grafana.json to match the dashboard specification
in docs/specs/grafana-dashboard-specs.md.

Run: python scripts/build_grafana_dashboard.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "dashboards" / "greenkube-grafana.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROM_DS = {"type": "datasource", "uid": "${DS_PROMETHEUS}"}
# Use regex-match (~=) so that Grafana's "All" value (".*") works correctly.
# An equality match (=) with ".*" would match the literal string ".*", finding nothing.
CLUSTER_FILTER = 'cluster=~"$cluster"'
NS_FILTER = 'cluster=~"$cluster", namespace=~"$namespace"'
NODE_FILTER = 'node=~"$node"'


def stat(pid, title, expr, unit="short", color=None, thresholds=None, gridpos=None, timeFrom=None):
    """Stat panel.

    By default uses the full dashboard time range (timeFrom=None).
    Pass timeFrom='5m' only for panels that must always reflect the current cluster
    state regardless of the selected window (e.g. live scores / counts).
    """
    panel = {
        "id": pid,
        "type": "stat",
        "title": title,
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 4, "h": 4},
        "datasource": PROM_DS,
        "targets": [{"datasource": PROM_DS, "expr": expr, "refId": "A", "range": True, "instant": False}],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "auto",
            "textMode": "auto",
            "colorMode": "background",
            "graphMode": "none",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": color or "thresholds"},
                "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "blue", "value": None}]},
            },
            "overrides": [],
        },
    }
    if timeFrom is not None:
        panel["timeFrom"] = timeFrom
    return panel


def gauge(pid, title, expr, unit="short", min_val=0, max_val=100, thresholds=None, gridpos=None, timeFrom=None):
    panel = {
        "id": pid,
        "type": "gauge",
        "title": title,
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 4, "h": 8},
        "datasource": PROM_DS,
        "targets": [{"datasource": PROM_DS, "expr": expr, "refId": "A", "range": True, "instant": False}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "orientation": "auto", "showThresholdLabels": False},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": min_val,
                "max": max_val,
                "thresholds": thresholds
                or {
                    "mode": "absolute",
                    "steps": [
                        {"color": "red", "value": None},
                        {"color": "orange", "value": 40},
                        {"color": "green", "value": 70},
                    ],
                },
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
    }
    if timeFrom is not None:
        panel["timeFrom"] = timeFrom
    return panel


def bargauge(
    pid, title, expr, legend, unit="short", thresholds=None, gridpos=None, instant=False, displayMode="gradient"
):
    return {
        "id": pid,
        "type": "bargauge",
        "title": title,
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 8, "h": 8},
        "datasource": PROM_DS,
        "targets": [
            {
                "datasource": PROM_DS,
                "expr": expr,
                "legendFormat": legend,
                "refId": "A",
                "range": not instant,
                "instant": instant,
            }
        ],
        "options": {
            "reduceOptions": {"calcs": [], "fields": "", "values": True},
            "orientation": "horizontal",
            "displayMode": displayMode,
            "valueMode": "color",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "transformations": [
            {"id": "reduce", "options": {"reducers": ["lastNotNull"]}},
            {"id": "sortBy", "options": {"fields": [{"displayName": "Last *", "desc": True}]}},
        ],
    }


def timeseries(pid, title, targets, unit="short", gridpos=None):
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 12, "h": 8},
        "datasource": PROM_DS,
        "targets": [
            {
                "datasource": PROM_DS,
                "expr": t["expr"],
                "legendFormat": t.get("legend", "{{pod}}"),
                "refId": t.get("refId", "A"),
                "range": True,
                "instant": False,
            }
            for t in targets
        ],
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"displayMode": "list", "placement": "bottom"},
        },
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 10}},
            "overrides": [],
        },
    }


def piechart(pid, title, expr, legend, unit="short", gridpos=None):
    return {
        "id": pid,
        "type": "piechart",
        "title": title,
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 8, "h": 8},
        "datasource": PROM_DS,
        "targets": [
            {"datasource": PROM_DS, "expr": expr, "legendFormat": legend, "refId": "A", "range": True, "instant": False}
        ],
        "options": {
            "pieType": "donut",
            "displayLabels": ["name", "percent"],
            "legend": {"displayMode": "table", "placement": "right"},
        },
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
    }


def table(pid, title, targets, gridpos=None):
    return {
        "id": pid,
        "type": "table",
        "title": title,
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 24, "h": 8},
        "datasource": PROM_DS,
        "targets": [
            {
                "datasource": PROM_DS,
                "expr": t["expr"],
                "legendFormat": t.get("legend", ""),
                "refId": t.get("refId", "A"),
                "range": True,
                "instant": False,
                "format": "table",
            }
            for t in targets
        ],
        "options": {"sortBy": [], "footer": {"show": False}},
        "fieldConfig": {"defaults": {}, "overrides": []},
        "transformations": [{"id": "merge", "options": {}}],
    }


def row(pid, title, collapsed=False, y=0):
    return {
        "id": pid,
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


# ---------------------------------------------------------------------------
# Thresholds reused across panels
# ---------------------------------------------------------------------------
CO2_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "green", "value": None},
        {"color": "orange", "value": 50000},
        {"color": "red", "value": 200000},
    ],
}
COST_THRESHOLDS = {
    "mode": "absolute",
    "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 50}, {"color": "red", "value": 200}],
}
EFFICIENCY_THRESHOLDS = {
    "mode": "absolute",
    "steps": [{"color": "red", "value": None}, {"color": "orange", "value": 0.3}, {"color": "green", "value": 0.6}],
}
SCORE_THRESHOLDS = {
    "mode": "absolute",
    "steps": [{"color": "red", "value": None}, {"color": "orange", "value": 40}, {"color": "green", "value": 70}],
}
SAVINGS_THRESHOLDS = {"mode": "absolute", "steps": [{"color": "blue", "value": None}, {"color": "green", "value": 1}]}

# ---------------------------------------------------------------------------
# Dashboard definition
# ---------------------------------------------------------------------------

panels = []
y = 0  # running y position tracker

# ── Row 0: Sustainability Command Center ──────────────────────────────────
panels.append({**row(100, "Sustainability Command Center", collapsed=False, y=y)})
y += 1

# Big sustainability score gauge on the left
panels.append(
    gauge(
        101,
        "Sustainability Score",
        f"avg(max by (cluster) (greenkube_sustainability_score{{{CLUSTER_FILTER}}}))",
        unit="none",
        min_val=0,
        max_val=100,
        thresholds=SCORE_THRESHOLDS,
        gridpos={"x": 0, "y": y, "w": 4, "h": 12},
        timeFrom="5m",
    )
)

# Sub-row A: Current footprint (3 panels, widths 7+7+6=20 to fill the 20-col space)
_row_a = [
    (
        "Total CO₂e (Scope 2)",
        f"sum(max by (cluster) (greenkube_cluster_co2e_grams_total{{{CLUSTER_FILTER}}}))",
        "g CO₂e",
        CO2_THRESHOLDS,
    ),
    (
        "Total CO₂e (Scope 3 Embodied)",
        f"sum(max by (cluster) (greenkube_cluster_embodied_co2e_grams_total{{{CLUSTER_FILTER}}}))",
        "g CO₂e",
        CO2_THRESHOLDS,
    ),
    (
        "Total Cloud Cost",
        f"sum(max by (cluster) (greenkube_cluster_cost_dollars_total{{{CLUSTER_FILTER}}}))",
        "currencyUSD",
        COST_THRESHOLDS,
    ),
]
_row_widths = [7, 7, 6]
_row_xs = [4, 11, 18]
for i, (title, expr, unit, thresh) in enumerate(_row_a):
    panels.append(
        stat(
            102 + i,
            title,
            expr,
            unit=unit,
            thresholds=thresh,
            gridpos={"x": _row_xs[i], "y": y, "w": _row_widths[i], "h": 4},
        )
    )

# Sub-row B: GreenKube impact — window-aware attributed savings via increase()
# timeFrom=None so increase() panels respect the full dashboard time range.
_co2_saved_expr = f"sum(increase(greenkube_co2e_savings_attributed_grams_total{{{CLUSTER_FILTER}}}[$__range]))"
_cost_saved_expr = f"sum(increase(greenkube_cost_savings_attributed_dollars_total{{{CLUSTER_FILTER}}}[$__range]))"
_row_b = [
    ("CO₂e Avoided (selected window)", _co2_saved_expr, "g CO₂e", None),
    ("Cost Avoided (selected window)", _cost_saved_expr, "currencyUSD", None),
    (
        "Recommendations Implemented",
        f"sum(greenkube_recommendations_implemented_total{{{CLUSTER_FILTER}}})",
        "short",
        None,
    ),
]
for i, (title, expr, unit, _) in enumerate(_row_b):
    panels.append(
        stat(
            106 + i,
            title,
            expr,
            unit=unit,
            thresholds=SAVINGS_THRESHOLDS,
            gridpos={"x": _row_xs[i], "y": y + 4, "w": _row_widths[i], "h": 4},
        )
    )

# Sub-row C: Where to act next (3 panels)
_total_recs_expr = (
    f"sum(max by (cluster, namespace, type, priority) (greenkube_recommendations_total{{{CLUSTER_FILTER}}}))"
)
_rec_types_expr = f"count(max by (cluster, type) (greenkube_recommendations_total{{{CLUSTER_FILTER}}}))"
_top_ns_co2_expr = f"topk(1, sum by (namespace) (greenkube_namespace_co2e_grams_total{{{CLUSTER_FILTER}}}))"

_row_c = [
    ("Active Recommendations", _total_recs_expr, "short", None),
    ("Active Rec. Types", _rec_types_expr, "short", None),
    ("#1 Namespace by CO₂e", _top_ns_co2_expr, "short", "{{namespace}}"),
]
for i, (title, expr, unit, legend) in enumerate(_row_c):
    p = stat(
        110 + i,
        title,
        expr,
        unit=unit,
        thresholds=SAVINGS_THRESHOLDS,
        gridpos={"x": _row_xs[i], "y": y + 8, "w": _row_widths[i], "h": 4},
    )
    if legend:
        p["targets"][0]["legendFormat"] = legend
        p["targets"][0]["instant"] = True
        p["targets"][0]["range"] = False
        p["options"]["textMode"] = "name"
    panels.append(p)

y += 12

# Top 3 namespace bar gauges — instant queries so topk returns exactly 3 in sorted order.
# sort_desc() ensures descending order (highest bar at top) in the Prometheus response.
_top3_co2e_expr = f"sort_desc(topk(3, sum by (namespace) (greenkube_namespace_co2e_grams_total{{{CLUSTER_FILTER}}})))"
_top3_cost_expr = f"sort_desc(topk(3, sum by (namespace) (greenkube_namespace_cost_dollars_total{{{CLUSTER_FILTER}}})))"
_top3_types_expr = (
    f"topk(3, sum by (type)"
    f" (max by (cluster, namespace, type, priority)"
    f" (greenkube_recommendations_total{{{CLUSTER_FILTER}}})))"
)
for i, (title, expr, legend, unit, instant, disp) in enumerate(
    [
        ("Top 3 Namespaces — CO₂e", _top3_co2e_expr, "{{namespace}}", "g CO₂e", True, "gradient"),
        ("Top 3 Namespaces — Cost", _top3_cost_expr, "{{namespace}}", "currencyUSD", True, "gradient"),
        ("Top 3 Recommendation Types", _top3_types_expr, "{{type}}", "short", True, "gradient"),
    ]
):
    panels.append(
        bargauge(
            114 + i,
            title,
            expr,
            legend,
            unit=unit,
            instant=instant,
            displayMode=disp,
            gridpos={"x": i * 8, "y": y, "w": 8, "h": 6},
        )
    )
y += 6

# ── Row 1: Carbon, Cost & Trends ──────────────────────────────────────────
panels.append({**row(200, "Carbon, Cost & Energy Trends", collapsed=True, y=y)})
y += 1

for i, (title, expr, unit) in enumerate(
    [
        ("CO₂e Over Time", f"sum(max by (cluster) (greenkube_cluster_co2e_grams_total{{{CLUSTER_FILTER}}}))", "g CO₂e"),
        (
            "Cloud Cost Over Time",
            f"sum(max by (cluster) (greenkube_cluster_cost_dollars_total{{{CLUSTER_FILTER}}}))",
            "currencyUSD",
        ),
        (
            "Energy Over Time (kWh)",
            f"sum(max by (cluster) (greenkube_cluster_energy_joules_total{{{CLUSTER_FILTER}}})) / 3600000",
            "kWh",
        ),
        (
            "Grid Carbon Intensity by Zone",
            f"avg by (zone) (max by (cluster, zone) (greenkube_carbon_intensity_zone{{{CLUSTER_FILTER}}}))",
            "gCO₂/kWh",
        ),
    ]
):
    panels.append(
        timeseries(
            201 + i,
            title,
            [{"expr": expr, "legend": "{{zone}}" if i == 3 else "cluster"}],
            unit=unit,
            gridpos={"x": (i % 2) * 12, "y": y + (i // 2) * 8, "w": 12, "h": 8},
        )
    )
y += 16

# ── Row 2: Sustainability Score Breakdown ─────────────────────────────────
panels.append({**row(300, "Sustainability Score Breakdown", collapsed=True, y=y)})
y += 1

panels.append(
    bargauge(
        301,
        "Score by Dimension",
        (
            f"avg by (dimension)"
            f" (max by (cluster, dimension)"
            f" (greenkube_sustainability_dimension_score{{{CLUSTER_FILTER}}}))"
        ),
        "{{dimension}}",
        unit="none",
        thresholds=SCORE_THRESHOLDS,
        gridpos={"x": 0, "y": y, "w": 12, "h": 8},
    )
)
panels.append(
    timeseries(
        302,
        "Sustainability Score Over Time",
        [{"expr": f"avg(max by (cluster) (greenkube_sustainability_score{{{CLUSTER_FILTER}}}))", "legend": "Average"}],
        unit="none",
        gridpos={"x": 12, "y": y, "w": 8, "h": 8},
    )
)
panels.append(
    gauge(
        303,
        "Data Quality — Measured Coverage",
        f"avg(max by (cluster) ((1 - greenkube_estimated_metrics_ratio{{{CLUSTER_FILTER}}}))) * 100",
        unit="percent",
        min_val=0,
        max_val=100,
        thresholds={
            "mode": "absolute",
            "steps": [
                {"color": "red", "value": None},
                {"color": "orange", "value": 50},
                {"color": "green", "value": 80},
            ],
        },
        gridpos={"x": 20, "y": y, "w": 4, "h": 8},
    )
)
y += 8

# ── Row 3: Resource Efficiency ────────────────────────────────────────────
panels.append({**row(400, "Resource Efficiency", collapsed=True, y=y)})
y += 1

panels.append(
    bargauge(
        401,
        "CPU Efficiency by Namespace",
        (
            f"sum by (namespace) (greenkube_pod_cpu_usage_millicores{{{NS_FILTER}}})"
            f" / sum by (namespace) (greenkube_pod_cpu_request_millicores{{{NS_FILTER}}})"
        ),
        "{{namespace}}",
        unit="percentunit",
        thresholds=EFFICIENCY_THRESHOLDS,
        gridpos={"x": 0, "y": y, "w": 12, "h": 8},
    )
)
panels.append(
    bargauge(
        402,
        "Memory Efficiency by Namespace",
        (
            f"sum by (namespace) (greenkube_pod_memory_usage_bytes{{{NS_FILTER}}})"
            f" / sum by (namespace) (greenkube_pod_memory_request_bytes{{{NS_FILTER}}})"
        ),
        "{{namespace}}",
        unit="percentunit",
        thresholds=EFFICIENCY_THRESHOLDS,
        gridpos={"x": 12, "y": y, "w": 12, "h": 8},
    )
)
y += 8

panels.append(
    bargauge(
        403,
        "Top 20 Pods — CPU Efficiency Ratio (worst first)",
        f"sort_desc(max by (namespace, pod) (greenkube_pod_cpu_efficiency_ratio{{{NS_FILTER}}}))",
        "{{namespace}}/{{pod}}",
        unit="percentunit",
        thresholds=EFFICIENCY_THRESHOLDS,
        gridpos={"x": 0, "y": y, "w": 12, "h": 10},
    )
)
panels.append(
    bargauge(
        404,
        "Top 20 Pods — Memory Efficiency Ratio (worst first)",
        f"sort_desc(max by (namespace, pod) (greenkube_pod_memory_efficiency_ratio{{{NS_FILTER}}}))",
        "{{namespace}}/{{pod}}",
        unit="percentunit",
        thresholds=EFFICIENCY_THRESHOLDS,
        gridpos={"x": 12, "y": y, "w": 12, "h": 10},
    )
)
y += 10

# ── Row 4: Namespace Analysis ─────────────────────────────────────────────
panels.append({**row(500, "Namespace Analysis", collapsed=True, y=y)})
y += 1

panels.append(
    piechart(
        501,
        "CO₂e by Namespace",
        f"sum by (namespace) (greenkube_namespace_co2e_grams_total{{{CLUSTER_FILTER}}})",
        "{{namespace}}",
        unit="g CO₂e",
        gridpos={"x": 0, "y": y, "w": 8, "h": 8},
    )
)
panels.append(
    piechart(
        502,
        "Cost by Namespace",
        f"sum by (namespace) (greenkube_namespace_cost_dollars_total{{{CLUSTER_FILTER}}})",
        "{{namespace}}",
        unit="currencyUSD",
        gridpos={"x": 8, "y": y, "w": 8, "h": 8},
    )
)
panels.append(
    piechart(
        503,
        "Energy by Namespace (kWh)",
        f"sum by (namespace) (greenkube_namespace_energy_joules_total{{{CLUSTER_FILTER}}}) / 3600000",
        "{{namespace}}",
        unit="kWh",
        gridpos={"x": 16, "y": y, "w": 8, "h": 8},
    )
)
y += 8

panels.append(
    table(
        504,
        "Namespace Summary",
        [
            {
                "expr": f"sum by (namespace) (greenkube_namespace_co2e_grams_total{{{CLUSTER_FILTER}}})",
                "legend": "CO₂e (g)",
                "refId": "A",
            },
            {
                "expr": f"sum by (namespace) (greenkube_namespace_embodied_co2e_grams_total{{{CLUSTER_FILTER}}})",
                "legend": "Embodied CO₂e (g)",
                "refId": "B",
            },
            {
                "expr": f"sum by (namespace) (greenkube_namespace_cost_dollars_total{{{CLUSTER_FILTER}}})",
                "legend": "Cost ($)",
                "refId": "C",
            },
            {
                "expr": f"sum by (namespace) (greenkube_namespace_energy_joules_total{{{CLUSTER_FILTER}}}) / 3600000",
                "legend": "Energy (kWh)",
                "refId": "D",
            },
            {
                "expr": f"max by (namespace) (greenkube_namespace_pod_count{{{CLUSTER_FILTER}}})",
                "legend": "Pods",
                "refId": "E",
            },
            {
                "expr": (
                    f"sum by (namespace)"
                    f" (max by (cluster, namespace, type, priority)"
                    f" (greenkube_recommendations_total{{{CLUSTER_FILTER}}}))"
                ),
                "legend": "Active Recommendations",
                "refId": "F",
            },
        ],
        gridpos={"x": 0, "y": y, "w": 24, "h": 8},
    )
)
y += 8

# ── Row 5: Top Emitters & Spenders ───────────────────────────────────────
panels.append({**row(600, "Top Emitters & Spenders", collapsed=True, y=y)})
y += 1

for i, (title, expr, unit) in enumerate(
    [
        (
            "Top 15 Pods — CO₂e",
            f"topk(15, max by (namespace, pod) (greenkube_pod_co2e_grams{{{NS_FILTER}}}))",
            "g CO₂e",
        ),
        (
            "Top 15 Pods — Cloud Cost",
            f"topk(15, max by (namespace, pod) (greenkube_pod_cost_dollars{{{NS_FILTER}}}))",
            "currencyUSD",
        ),
        (
            "Top 15 Pods — Energy",
            f"topk(15, max by (namespace, pod) (greenkube_pod_energy_joules{{{NS_FILTER}}})) / 3600000",
            "kWh",
        ),
        (
            "Top 15 Pods — Embodied CO₂e (Scope 3)",
            f"topk(15, max by (namespace, pod) (greenkube_pod_embodied_co2e_grams{{{NS_FILTER}}}))",
            "g CO₂e",
        ),
    ]
):
    panels.append(
        bargauge(
            601 + i,
            title,
            expr,
            "{{namespace}}/{{pod}}",
            unit=unit,
            gridpos={"x": (i % 2) * 12, "y": y + (i // 2) * 10, "w": 12, "h": 10},
        )
    )
y += 20

# ── Row 6: Node Analysis ──────────────────────────────────────────────────
panels.append({**row(700, "Node Analysis", collapsed=True, y=y)})
y += 1

panels.append(
    bargauge(
        701,
        "Node CPU Allocation Ratio",
        (
            f"max by (node) (greenkube_node_cpu_allocated_millicores{{{NODE_FILTER}}})"
            f" / max by (node) (greenkube_node_cpu_capacity_millicores{{{NODE_FILTER}}})"
        ),
        "{{node}}",
        unit="percentunit",
        thresholds={
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "orange", "value": 0.7},
                {"color": "red", "value": 0.9},
            ],
        },
        gridpos={"x": 0, "y": y, "w": 8, "h": 8},
    )
)
panels.append(
    bargauge(
        702,
        "Node Memory Allocation Ratio",
        (
            f"max by (node) (greenkube_node_memory_allocated_bytes{{{NODE_FILTER}}})"
            f" / max by (node) (greenkube_node_memory_capacity_bytes{{{NODE_FILTER}}})"
        ),
        "{{node}}",
        unit="percentunit",
        thresholds={
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "orange", "value": 0.7},
                {"color": "red", "value": 0.9},
            ],
        },
        gridpos={"x": 8, "y": y, "w": 8, "h": 8},
    )
)
panels.append(
    bargauge(
        703,
        "Node Embodied Emissions (kg CO₂e)",
        f"max by (node) (greenkube_node_embodied_emissions_kg{{{NODE_FILTER}}})",
        "{{node}}",
        unit="kg CO₂e",
        gridpos={"x": 16, "y": y, "w": 8, "h": 8},
    )
)
y += 8

panels.append(
    table(
        704,
        "Node Inventory",
        [
            {
                "expr": (
                    f"max by (node, cluster, architecture, cloud_provider,"
                    f" instance_type, region, zone)"
                    f" (greenkube_node_info{{{NODE_FILTER}}})"
                ),
                "legend": "",
                "refId": "A",
            }
        ],
        gridpos={"x": 0, "y": y, "w": 24, "h": 8},
    )
)
y += 8

# ── Row 7: Network & Storage I/O ──────────────────────────────────────────
panels.append({**row(800, "Network & Storage I/O", collapsed=True, y=y)})
y += 1

for i, (title, expr) in enumerate(
    [
        (
            "Top 10 Pods — Network Receive (B/s)",
            f"topk(10, max by (namespace, pod) (greenkube_pod_network_receive_bytes{{{NS_FILTER}}}))",
        ),
        (
            "Top 10 Pods — Network Transmit (B/s)",
            f"topk(10, max by (namespace, pod) (greenkube_pod_network_transmit_bytes{{{NS_FILTER}}}))",
        ),
        (
            "Top 10 Pods — Disk Read (B/s)",
            f"topk(10, max by (namespace, pod) (greenkube_pod_disk_read_bytes{{{NS_FILTER}}}))",
        ),
        (
            "Top 10 Pods — Disk Write (B/s)",
            f"topk(10, max by (namespace, pod) (greenkube_pod_disk_write_bytes{{{NS_FILTER}}}))",
        ),
    ]
):
    panels.append(
        timeseries(
            801 + i,
            title,
            [{"expr": expr, "legend": "{{namespace}}/{{pod}}"}],
            unit="Bps",
            gridpos={"x": (i % 2) * 12, "y": y + (i // 2) * 8, "w": 12, "h": 8},
        )
    )
y += 16

# ── Row 8: Pod Stability ──────────────────────────────────────────────────
panels.append({**row(900, "Pod Stability", collapsed=True, y=y)})
y += 1

panels.append(
    bargauge(
        901,
        "Top 15 Restarting Pods",
        f"topk(15, max by (namespace, pod) (greenkube_pod_restart_count{{{NS_FILTER}}}))",
        "{{namespace}}/{{pod}}",
        unit="short",
        thresholds={
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "orange", "value": 5},
                {"color": "red", "value": 20},
            ],
        },
        gridpos={"x": 0, "y": y, "w": 12, "h": 10},
    )
)
panels.append(
    bargauge(
        902,
        "Restarts by Namespace",
        f"sum by (namespace) (greenkube_pod_restart_count{{{NS_FILTER}}})",
        "{{namespace}}",
        unit="short",
        thresholds={
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "orange", "value": 10},
                {"color": "red", "value": 50},
            ],
        },
        gridpos={"x": 12, "y": y, "w": 12, "h": 10},
    )
)
y += 10

# ── Row 9: Recommendations & Savings Potential ────────────────────────────
panels.append({**row(1000, "Recommendations & Savings", collapsed=True, y=y)})
y += 1

# Stat row
for i, (title, expr, unit, use_full_range) in enumerate(
    [
        (
            "Active Recommendations",
            f"sum(max by (cluster, namespace, type, priority) (greenkube_recommendations_total{{{CLUSTER_FILTER}}}))",
            "short",
            False,
        ),
        (
            "CO₂e Avoided (selected window)",
            f"sum(increase(greenkube_co2e_savings_attributed_grams_total{{{CLUSTER_FILTER}}}[$__range]))",
            "g CO₂e",
            True,
        ),
        (
            "Cost Avoided (selected window)",
            f"sum(increase(greenkube_cost_savings_attributed_dollars_total{{{CLUSTER_FILTER}}}[$__range]))",
            "currencyUSD",
            True,
        ),
        (
            "Recommendations Implemented",
            f"sum(greenkube_recommendations_implemented_total{{{CLUSTER_FILTER}}})",
            "short",
            False,
        ),
        (
            "Open Recommendations",
            f"sum(max by (cluster, namespace, type, priority) (greenkube_recommendations_total{{{CLUSTER_FILTER}}}))",
            "short",
            False,
        ),
    ]
):
    panels.append(
        stat(
            1001 + i,
            title,
            expr,
            unit=unit,
            thresholds=SAVINGS_THRESHOLDS,
            gridpos={"x": i * 4, "y": y, "w": 4, "h": 4},
            timeFrom=None if use_full_range else "5m",
        )
    )
y += 4

panels.append(
    bargauge(
        1010,
        "Recommendations by Type",
        (
            f"sum by (type)"
            f" (max by (cluster, namespace, type, priority)"
            f" (greenkube_recommendations_total{{{CLUSTER_FILTER}}}))"
        ),
        "{{type}}",
        unit="short",
        gridpos={"x": 0, "y": y, "w": 12, "h": 8},
    )
)
panels.append(
    bargauge(
        1011,
        "CO₂e Avoided by Recommendation Type (projected/year)",
        f"sum by (type) (max by (cluster, type) (greenkube_cluster_co2e_saved_grams_total{{{CLUSTER_FILTER}}}))",
        "{{type}}",
        unit="g CO₂e",
        gridpos={"x": 12, "y": y, "w": 12, "h": 8},
    )
)
y += 8

# ── Row 10: GreenKube Self-Monitoring ─────────────────────────────────────
panels.append({**row(1100, "GreenKube Self-Monitoring", collapsed=True, y=y)})
y += 1

for i, (title, expr, unit, thresh) in enumerate(
    [
        (
            "Last Collection (s ago)",
            f"time() - max(max by (cluster) (greenkube_last_collection_timestamp_seconds{{{CLUSTER_FILTER}}}))",
            "s",
            {
                "mode": "absolute",
                "steps": [
                    {"color": "green", "value": None},
                    {"color": "orange", "value": 300},
                    {"color": "red", "value": 600},
                ],
            },
        ),
        ("Metrics in Window", f"sum(max by (cluster) (greenkube_metrics_total{{{CLUSTER_FILTER}}}))", "short", None),
        (
            "Estimated Metrics Ratio",
            f"avg(max by (cluster) (greenkube_estimated_metrics_ratio{{{CLUSTER_FILTER}}})) * 100",
            "percent",
            {
                "mode": "absolute",
                "steps": [
                    {"color": "green", "value": None},
                    {"color": "orange", "value": 30},
                    {"color": "red", "value": 60},
                ],
            },
        ),
        (
            "Active Namespaces",
            f"sum(max by (cluster) (greenkube_cluster_namespace_count{{{CLUSTER_FILTER}}}))",
            "short",
            None,
        ),
        (
            "Carbon Intensity Score (avg)",
            f"avg(max by (cluster) (greenkube_carbon_intensity_score{{{CLUSTER_FILTER}}}))",
            "short",
            {
                "mode": "absolute",
                "steps": [
                    {"color": "green", "value": None},
                    {"color": "orange", "value": 200},
                    {"color": "red", "value": 400},
                ],
            },
        ),
        (
            "PUE (avg)",
            f"avg(max by (cluster, namespace) (greenkube_pue{{{NS_FILTER}}}))",
            "none",
            {
                "mode": "absolute",
                "steps": [
                    {"color": "green", "value": None},
                    {"color": "orange", "value": 1.5},
                    {"color": "red", "value": 2.0},
                ],
            },
        ),
    ]
):
    panels.append(
        stat(
            1101 + i,
            title,
            expr,
            unit=unit,
            thresholds=thresh or {"mode": "absolute", "steps": [{"color": "blue", "value": None}]},
            gridpos={"x": i * 4, "y": y, "w": 4, "h": 4},
        )
    )
y += 4

# ---------------------------------------------------------------------------
# Assemble dashboard
# ---------------------------------------------------------------------------

DASHBOARD = {
    "__inputs": [
        {
            "name": "DS_PROMETHEUS",
            "label": "Prometheus",
            "description": "Prometheus datasource scraping GreenKube /prometheus/metrics",
            "type": "datasource",
            "pluginId": "prometheus",
            "pluginName": "Prometheus",
        }
    ],
    "__requires": [
        {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"},
        {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"},
        {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
        {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
        {"type": "panel", "id": "table", "name": "Table", "version": ""},
        {"type": "panel", "id": "piechart", "name": "Pie chart", "version": ""},
        {"type": "panel", "id": "bargauge", "name": "Bar gauge", "version": ""},
        {"type": "panel", "id": "gauge", "name": "Gauge", "version": ""},
    ],
    "id": None,
    "uid": "greenkube-fingreenops",
    "title": "GreenKube FinGreenOps Dashboard",
    "description": (
        "FinGreenOps dashboard for Kubernetes — sustainability score, CO₂ emissions (Scope 2 & 3), "
        "cloud costs, resource efficiency, node analysis, pod stability, recommendations, and realized savings."
    ),
    "tags": ["greenkube", "fingreenops", "carbon", "cost", "kubernetes", "sustainability"],
    "style": "dark",
    "timezone": "utc",
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "time": {"from": "now-7d", "to": "now"},
    "refresh": "5m",
    "schemaVersion": 39,
    "version": 3,
    "links": [
        {
            "title": "GreenKube Docs",
            "url": "https://github.com/GreenKubeCloud/GreenKube",
            "type": "link",
            "icon": "external link",
            "targetBlank": True,
        }
    ],
    "templating": {
        "list": [
            {
                "name": "DS_PROMETHEUS",
                "type": "datasource",
                "pluginId": "prometheus",
                "query": "prometheus",
                "label": "Prometheus",
                "hide": 0,
                "refresh": 1,
                "current": {"text": "prometheus", "value": "P1809F7CD0C75ACF3"},
            },
            {
                "name": "cluster",
                "type": "query",
                "datasource": PROM_DS,
                "label": "Cluster",
                "query": "label_values(greenkube_cluster_co2e_grams_total, cluster)",
                "refresh": 2,
                "sort": 1,
                "multi": False,
                "includeAll": True,
                "allValue": ".*",
                "hide": 0,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "namespace",
                "type": "query",
                "datasource": PROM_DS,
                "label": "Namespace",
                "query": 'label_values(greenkube_namespace_co2e_grams_total{cluster="$cluster"}, namespace)',
                "refresh": 2,
                "sort": 1,
                "multi": True,
                "includeAll": True,
                "allValue": ".*",
                "hide": 0,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "node",
                "type": "query",
                "datasource": PROM_DS,
                "label": "Node",
                "query": "label_values(greenkube_node_info, node)",
                "refresh": 2,
                "sort": 1,
                "multi": True,
                "includeAll": True,
                "allValue": ".*",
                "hide": 0,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "region",
                "type": "query",
                "datasource": PROM_DS,
                "label": "Region",
                "query": "label_values(greenkube_pod_co2e_grams, region)",
                "refresh": 2,
                "sort": 1,
                "multi": True,
                "includeAll": True,
                "allValue": ".*",
                "hide": 0,
                "current": {"text": "All", "value": "$__all"},
            },
        ]
    },
    "annotations": {"list": []},
    "panels": panels,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(DASHBOARD, indent=2, ensure_ascii=False))
    print(f"Written {len(panels)} panels to {OUT}")
