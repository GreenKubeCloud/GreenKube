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
PROMETHEUS_DEFAULT_UID = "P1809F7CD0C75ACF3"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROM_DS = {"type": "datasource", "uid": "${DS_PROMETHEUS}"}
# Use regex-match (~=) so that Grafana's "All" value (".*") works correctly.
# An equality match (=) with ".*" would match the literal string ".*", finding nothing.
CLUSTER_FILTER = 'cluster=~"$cluster"'
NS_FILTER = 'cluster=~"$cluster", namespace=~"$namespace"'
NODE_FILTER = 'node=~"$node"'


def stat(
    pid,
    title,
    expr,
    unit="short",
    color=None,
    thresholds=None,
    gridpos=None,
    timeFrom=None,
    graphMode="none",
    textMode="auto",
    wideLayout=False,
):
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
            "textMode": textMode,
            "colorMode": "background",
            "graphMode": graphMode,
            "wideLayout": wideLayout,
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


def echarts_panel(pid, title, targets, get_option, gridpos=None, description=""):
    return {
        "id": pid,
        "type": "volkovlabs-echarts-panel",
        "title": title,
        "description": description,
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 8, "h": 8},
        "datasource": PROM_DS,
        "transparent": True,
        "targets": [
            {
                "datasource": PROM_DS,
                "expr": target["expr"],
                "legendFormat": target.get("legend", target.get("legendFormat", "")),
                "refId": target.get("refId", chr(65 + index)),
                "range": target.get("range", True),
                "instant": target.get("instant", False),
            }
            for index, target in enumerate(targets)
        ],
        "options": {
            "renderer": "canvas",
            "map": "none",
            "editorMode": "code",
            "editor": {"format": "auto"},
            "themeEditor": {"name": "default", "config": "{}"},
            "baidu": {"key": "", "callback": "bmapReady"},
            "gaode": {"key": "", "plugin": "AMap.Scale,AMap.ToolBar"},
            "google": {"key": "", "callback": "gmapReady"},
            "visualEditor": {"dataset": [], "series": [], "code": "return {};"},
            "getOption": get_option,
        },
        "fieldConfig": {"defaults": {}, "overrides": []},
        "pluginVersion": "7.2.2",
    }


ECHARTS_THEME = """
const palette = {
    bg: '#020617',
    panel: 'rgba(15, 23, 42, 0.74)',
    panelSoft: 'rgba(15, 23, 42, 0.46)',
    cyan: '#00FFD4',
    teal: '#14B8A6',
    blue: '#38BDF8',
    green: '#34D399',
    amber: '#F59E0B',
    red: '#FB7185',
    text: '#E5F6FF',
    muted: '#94A3B8',
    grid: 'rgba(148, 163, 184, 0.16)',
};

const valuesOf = (field) => field?.values?.buffer || field?.values || [];

const matchesRefId = (series, refId) => series?.refId === refId
    || series?.meta?.custom?.refId === refId
    || series?.meta?.refId === refId
    || series?.name === refId;

const seriesForRef = (refId) => {
    const direct = context.panel.data.series.find((series) => matchesRefId(series, refId));
    if (direct) {
        return direct;
    }

    const fallbackIndex = refId.charCodeAt(0) - 'A'.charCodeAt(0);
    return context.panel.data.series[fallbackIndex];
};

const lastNumber = (series) => {
    const numberField = series?.fields.find((field) => field.type === 'number');
    const values = valuesOf(numberField);
    const rawValue = Number(values[values.length - 1] ?? 0);

    return Number.isFinite(rawValue) ? rawValue : 0;
};

const valueFor = (refId, clampToScore = false) => {
    const series = seriesForRef(refId);
    const value = lastNumber(series);

    return clampToScore ? Math.max(0, Math.min(100, value)) : value;
};

const pointsFor = (refId, labelKey, limit = 3) => context.panel.data.series
    .filter((series) => matchesRefId(series, refId))
    .map((series) => {
        const numberField = series.fields.find((field) => field.type === 'number');
        const label = numberField?.labels?.[labelKey] || series.name || labelKey;

        return { name: label, value: lastNumber(series) };
    })
    .filter((point) => Number.isFinite(point.value))
    .sort((a, b) => b.value - a.value)
    .slice(0, limit);

const compactNumber = (value) => {
    if (!Number.isFinite(value)) {
        return '0';
    }

    const absolute = Math.abs(value);
    if (absolute >= 1000000000) {
        return `${(value / 1000000000).toFixed(1)}B`;
    }
    if (absolute >= 1000000) {
        return `${(value / 1000000).toFixed(1)}M`;
    }
    if (absolute >= 1000) {
        return `${(value / 1000).toFixed(1)}k`;
    }

    return absolute >= 10 ? value.toFixed(0) : value.toFixed(1);
};

const formatValue = (value, unit) => {
    if (unit === 'currencyUSD') {
        const absolute = Math.abs(value);
        if (absolute > 0 && absolute < 0.01) {
            return '<$0.01';
        }

        const maximumFractionDigits = absolute >= 100 ? 0 : absolute >= 1 ? 2 : 3;
        return `$${value.toLocaleString(undefined, { maximumFractionDigits })}`;
    }
    if (unit === 'percent') {
        return `${value.toFixed(0)}%`;
    }
    if (unit === 'g CO₂e') {
        const absolute = Math.abs(value);
        if (absolute >= 1000000) {
            return `${(value / 1000000).toFixed(1)} t CO₂e`;
        }
        if (absolute >= 1000) {
            return `${(value / 1000).toFixed(1)} kg CO₂e`;
        }
        if (absolute >= 1) {
            return `${value.toFixed(1)} g CO₂e`;
        }
        if (absolute > 0) {
            return `${value.toFixed(3)} g CO₂e`;
        }

        return '0 g CO₂e';
    }
    if (unit === 'count') {
        return value.toFixed(0);
    }

    return compactNumber(value);
};
"""


ECHARTS_RADAR_OPTION = (
    ECHARTS_THEME
    + """
const dimensions = [
    { refId: 'A', name: 'Resource efficiency', max: 100 },
    { refId: 'B', name: 'Carbon efficiency', max: 100 },
    { refId: 'C', name: 'Waste elimination', max: 100 },
    { refId: 'D', name: 'Node efficiency', max: 100 },
    { refId: 'E', name: 'Scaling practices', max: 100 },
    { refId: 'F', name: 'Carbon aware', max: 100 },
    { refId: 'G', name: 'Stability', max: 100 },
];

const values = dimensions.map((dimension) => valueFor(dimension.refId, true));

const globalScore = valueFor('H', true);
const scoreColor = globalScore >= 80 ? palette.cyan : globalScore >= 60 ? palette.amber : palette.red;
const scoreTextColor = globalScore >= 80 ? '#D9FFF7' : globalScore >= 60 ? '#FFE6A6' : '#FFD6DE';

return {
    backgroundColor: 'transparent',
    color: [palette.cyan],
    tooltip: {
        trigger: 'item',
        confine: true,
        position: (point, params, dom, rect, size) => [size.viewSize[0] - size.contentSize[0] - 12, 12],
        backgroundColor: 'rgba(15, 23, 42, 0.96)',
        borderColor: palette.cyan,
        borderWidth: 1,
        padding: [10, 12],
        extraCssText: 'box-shadow: 0 18px 40px rgba(0, 0, 0, 0.35); border-radius: 8px;',
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: (params) => {
            const lines = dimensions.map((dimension, index) => {
                return `${dimension.name}: ${values[index].toFixed(0)}`;
            });

            return [`<strong>${params.name}</strong>`, ...lines].join('<br/>');
        },
    },
    legend: { show: false },
    radar: {
        center: ['50%', '52%'],
        radius: '68%',
        splitNumber: 4,
        shape: 'polygon',
        axisName: {
            color: palette.text,
            fontSize: 12,
            fontWeight: 600,
            padding: [2, 4],
        },
        axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.24)' } },
        splitLine: {
            lineStyle: {
                color: [
                    'rgba(0, 255, 212, 0.34)',
                    'rgba(148, 163, 184, 0.22)',
                    'rgba(148, 163, 184, 0.16)',
                    'rgba(148, 163, 184, 0.10)',
                ],
                width: 1,
            },
        },
        splitArea: {
            areaStyle: {
                color: ['rgba(0, 255, 212, 0.05)', 'rgba(15, 23, 42, 0.14)'],
            },
        },
        indicator: dimensions.map((dimension) => ({ name: dimension.name, max: dimension.max })),
    },
    series: [
        {
            name: 'Sustainability performance',
            type: 'radar',
            z: 4,
            label: { show: false },
            data: [
                {
                    name: 'Score',
                    value: values,
                    symbol: 'circle',
                    symbolSize: 6,
                    areaStyle: { color: 'rgba(0, 255, 212, 0.30)' },
                    lineStyle: { color: palette.cyan, width: 3 },
                    itemStyle: {
                        color: palette.cyan,
                        borderColor: 'rgba(255, 255, 255, 0.92)',
                        borderWidth: 1,
                    },
                },
            ],
            emphasis: {
                lineStyle: { width: 4 },
                areaStyle: { opacity: 0.35 },
                label: { show: false },
            },
        },
    ],
    graphic: [
        {
            type: 'text',
            left: 'center',
            top: 'middle',
            zlevel: 1000,
            z: 100000,
            silent: true,
            style: {
                text: String(Math.round(globalScore)),
                fill: scoreTextColor,
                fontSize: 36,
                fontWeight: 800,
                textAlign: 'center',
                textVerticalAlign: 'middle',
                shadowBlur: 24,
                shadowColor: scoreColor,
            },
        },
        {
            type: 'text',
            left: 'center',
            top: '55%',
            zlevel: 1000,
            z: 100000,
            silent: true,
            style: {
                text: '/ 100',
                fill: scoreTextColor,
                fontSize: 12,
                fontWeight: 700,
                textAlign: 'center',
                textVerticalAlign: 'top',
                shadowBlur: 14,
                shadowColor: scoreColor,
            },
        },
    ],
};
"""
)


ECHARTS_FOOTPRINT_MIX_OPTION = (
    ECHARTS_THEME
    + """
const rows = [
    { refId: 'A', name: 'Scope 2', unit: 'g CO₂e', color: palette.cyan },
    { refId: 'B', name: 'Scope 3', unit: 'g CO₂e', color: palette.blue },
    { refId: 'C', name: 'Cloud cost', unit: 'currencyUSD', color: palette.amber },
];

const data = rows.map((row) => {
    const raw = Math.max(0, valueFor(row.refId));
    return {
        name: row.name,
        value: Math.log10(raw + 1),
        raw,
        unit: row.unit,
        itemStyle: {
            color: {
                type: 'linear',
                x: 0,
                y: 1,
                x2: 0,
                y2: 0,
                colorStops: [
                    { offset: 0, color: `${row.color}44` },
                    { offset: 1, color: row.color },
                ],
            },
            borderRadius: [7, 7, 0, 0],
        },
        label: { formatter: () => formatValue(raw, row.unit) },
    };
});

return {
    backgroundColor: 'transparent',
    tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 23, 42, 0.96)',
        borderColor: palette.cyan,
        borderWidth: 1,
        textStyle: { color: palette.text },
        formatter: (params) => `${params.name}<br/><strong>${formatValue(params.data.raw, params.data.unit)}</strong>`,
    },
    grid: { top: 26, right: 18, bottom: 36, left: 18 },
    xAxis: {
        type: 'category',
        data: rows.map((row) => row.name),
        axisTick: { show: false },
        axisLine: { lineStyle: { color: palette.grid } },
        axisLabel: { color: palette.text, fontSize: 12, fontWeight: 600 },
    },
    yAxis: { type: 'value', show: false },
    series: [
        {
            name: 'Current footprint',
            type: 'bar',
            barWidth: '46%',
            data,
            label: {
                show: true,
                position: 'top',
                color: palette.text,
                fontSize: 12,
                fontWeight: 700,
            },
        },
    ],
};
"""
)


ECHARTS_IMPACT_LEDGER_OPTION = (
    ECHARTS_THEME
    + """
const rows = [
    { refId: 'A', name: 'CO₂e avoided', unit: 'g CO₂e', color: palette.cyan },
    { refId: 'B', name: 'Cost avoided', unit: 'currencyUSD', color: palette.green },
    { refId: 'C', name: 'Implemented', unit: 'count', color: palette.blue },
];

const data = rows.map((row) => {
    const raw = Math.max(0, valueFor(row.refId));
    const value = Math.log10(raw + 1) * 16;

    return {
        name: row.name,
        value,
        raw,
        unit: row.unit,
        itemStyle: { color: row.color, borderRadius: [0, 7, 7, 0] },
        label: { formatter: () => formatValue(raw, row.unit) },
    };
});

return {
    backgroundColor: 'transparent',
    tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 23, 42, 0.96)',
        borderColor: palette.cyan,
        borderWidth: 1,
        textStyle: { color: palette.text },
        formatter: (params) => `${params.name}<br/><strong>${formatValue(params.data.raw, params.data.unit)}</strong>`,
    },
    grid: { top: 18, right: 88, bottom: 10, left: 118 },
    xAxis: { type: 'value', show: false, max: (value) => Math.max(100, value.max * 1.08) },
    yAxis: {
        type: 'category',
        data: rows.map((row) => row.name).reverse(),
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { color: palette.text, fontSize: 12, fontWeight: 600 },
    },
    series: [
        {
            type: 'bar',
            barWidth: 12,
            data: data.reverse(),
            label: {
                show: true,
                position: 'right',
                color: palette.text,
                fontSize: 12,
                fontWeight: 700,
            },
        },
    ],
};
"""
)


ECHARTS_ACTION_PRIORITIES_OPTION = (
    ECHARTS_THEME
    + """
const groups = [
    { refId: 'A', title: 'CO₂e namespaces', labelKey: 'namespace', unit: 'g CO₂e', color: palette.cyan },
    { refId: 'B', title: 'Cost namespaces', labelKey: 'namespace', unit: 'currencyUSD', color: palette.amber },
    { refId: 'C', title: 'Recommendation types', labelKey: 'type', unit: 'count', color: palette.blue },
];

const grids = groups.map((group, index) => ({
    top: 36,
    bottom: 22,
    left: `${3 + index * 33}%`,
    width: '28%',
    containLabel: true,
}));

const pointGroups = groups.map((group) => pointsFor(group.refId, group.labelKey, 3));

return {
    backgroundColor: 'transparent',
    tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 23, 42, 0.96)',
        borderColor: palette.cyan,
        borderWidth: 1,
        textStyle: { color: palette.text },
        formatter: (params) => `${params.seriesName}<br/>${params.name}: <strong>${params.data.display}</strong>`,
    },
    title: groups.map((group, index) => ({
        text: group.title,
        left: `${5 + index * 33}%`,
        top: 6,
        textStyle: { color: palette.text, fontSize: 12, fontWeight: 700 },
    })),
    grid: grids,
    xAxis: groups.map((group, index) => ({
        type: 'value',
        gridIndex: index,
        show: false,
    })),
    yAxis: groups.map((group, index) => ({
        type: 'category',
        gridIndex: index,
        inverse: true,
        data: pointGroups[index].map((point) => point.name),
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { color: palette.muted, fontSize: 11, width: 82, overflow: 'truncate' },
    })),
    series: groups.map((group, index) => ({
        name: group.title,
        type: 'bar',
        xAxisIndex: index,
        yAxisIndex: index,
        barWidth: 10,
        data: pointGroups[index].map((point) => ({
            name: point.name,
            value: Math.max(0, Math.log10(point.value + 1)),
            raw: point.value,
            display: formatValue(point.value, group.unit),
            itemStyle: { color: group.color, borderRadius: [0, 6, 6, 0] },
        })),
        label: {
            show: true,
            position: 'right',
            color: palette.text,
            fontSize: 11,
            fontWeight: 700,
            formatter: (params) => params.data.display,
        },
    })),
};
"""
)


def sustainability_radar(pid, gridpos=None):
    dimensions = [
        ("resource_efficiency", "Resource efficiency"),
        ("carbon_efficiency", "Carbon efficiency"),
        ("waste_elimination", "Waste elimination"),
        ("node_efficiency", "Node efficiency"),
        ("scaling_practices", "Scaling practices"),
        ("carbon_aware_scheduling", "Carbon aware"),
        ("stability", "Stability"),
    ]
    dimension_targets = [
        {
            "datasource": PROM_DS,
            "expr": (
                "avg(max by (cluster, namespace) "
                f"(greenkube_sustainability_dimension_score{{{CLUSTER_FILTER}, "
                f'namespace=~"$namespace", namespace!="__all__", dimension="{dimension}"}}))'
            ),
            "legendFormat": label,
            "refId": chr(65 + index),
            "range": False,
            "instant": True,
        }
        for index, (dimension, label) in enumerate(dimensions)
    ]
    dimension_targets.append(
        {
            "datasource": PROM_DS,
            "expr": (
                "avg(max by (cluster, namespace) "
                f"(greenkube_sustainability_score{{{CLUSTER_FILTER}, "
                'namespace=~"$namespace", namespace!="__all__"}))'
            ),
            "legendFormat": "Global score",
            "refId": "H",
            "range": False,
            "instant": True,
        }
    )
    return {
        "id": pid,
        "type": "volkovlabs-echarts-panel",
        "title": "Sustainability Score Radar",
        "description": "Dynamic radar chart for the live GreenKube sustainability score dimensions.",
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 6, "h": 12},
        "datasource": PROM_DS,
        "transparent": True,
        "targets": dimension_targets,
        "options": {
            "renderer": "canvas",
            "map": "none",
            "editorMode": "code",
            "editor": {"format": "auto"},
            "themeEditor": {"name": "default", "config": "{}"},
            "baidu": {"key": "", "callback": "bmapReady"},
            "gaode": {"key": "", "plugin": "AMap.Scale,AMap.ToolBar"},
            "google": {"key": "", "callback": "gmapReady"},
            "visualEditor": {"dataset": [], "series": [], "code": "return {};"},
            "getOption": ECHARTS_RADAR_OPTION,
        },
        "fieldConfig": {
            "defaults": {
                "unit": "none",
                "min": 0,
                "max": 100,
                "thresholds": SCORE_THRESHOLDS,
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "pluginVersion": "7.2.2",
    }


def geomap(pid, title, targets, unit="gCO₂/kWh", gridpos=None):
    return {
        "id": pid,
        "type": "geomap",
        "title": title,
        "description": (
            "Regional node map colored by Electricity Maps grid intensity. Marker placement uses the EM zone "
            "country lookup, while node labels carry Kubernetes topology zone and region metadata."
        ),
        "gridPos": gridpos or {"x": 0, "y": 0, "w": 24, "h": 9},
        "datasource": PROM_DS,
        "targets": [
            {
                "datasource": PROM_DS,
                "expr": target["expr"],
                "legendFormat": target.get("legend", "{{node}} · {{region}}"),
                "refId": target.get("refId", "A"),
                "range": False,
                "instant": True,
                "format": "table",
            }
            for target in targets
        ],
        "options": {
            "view": {"id": "fit", "lat": 46, "lon": 2, "zoom": 3},
            "controls": {"showZoom": True, "mouseWheelZoom": False, "showAttribution": True, "showScale": True},
            "tooltip": {"mode": "details"},
            "basemap": {"type": "carto", "config": {"theme": "dark", "showLabels": True}},
            "layers": [
                {
                    "type": "markers",
                    "name": "Nodes by region",
                    "location": {
                        "mode": "lookup",
                        "lookup": "lookup",
                        "gazetteer": "/public/build/gazetteer/countries.json",
                    },
                    "config": {
                        "showLegend": True,
                        "style": {
                            "color": {"field": "Value", "fixed": "green"},
                            "opacity": 0.82,
                            "size": {"field": "bubble_size", "fixed": 18, "min": 14, "max": 56},
                            "symbol": {"mode": "fixed", "fixed": "build/img/icons/marker/circle.svg"},
                            "symbolAlign": {"horizontal": "center", "vertical": "center"},
                            "text": {"mode": "field", "field": "bubble_label", "fixed": ""},
                            "textConfig": {
                                "fontSize": 12,
                                "textAlign": "center",
                                "textBaseline": "middle",
                                "offsetX": 0,
                                "offsetY": 0,
                            },
                        },
                    },
                }
            ],
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "yellow", "value": 100},
                        {"color": "orange", "value": 250},
                        {"color": "red", "value": 500},
                    ],
                },
            },
            "overrides": [],
        },
        "transformations": [
            {
                "id": "convertFieldType",
                "options": {
                    "conversions": [
                        {"targetField": "node_count", "destinationType": "number"},
                        {"targetField": "bubble_size", "destinationType": "number"},
                    ]
                },
            }
        ],
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
        {"color": "#00FFD4", "value": None},
        {"color": "#F59E0B", "value": 50000},
        {"color": "#FB7185", "value": 200000},
    ],
}
COST_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "#00FFD4", "value": None},
        {"color": "#F59E0B", "value": 50},
        {"color": "#FB7185", "value": 200},
    ],
}
EFFICIENCY_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "#FB7185", "value": None},
        {"color": "#F59E0B", "value": 0.3},
        {"color": "#00FFD4", "value": 0.6},
    ],
}
SCORE_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "#FB7185", "value": None},
        {"color": "#F59E0B", "value": 60},
        {"color": "#00FFD4", "value": 80},
    ],
}
SAVINGS_THRESHOLDS = {
    "mode": "absolute",
    "steps": [{"color": "#38BDF8", "value": None}, {"color": "#00FFD4", "value": 1}],
}

# ---------------------------------------------------------------------------
# Dashboard definition
# ---------------------------------------------------------------------------

panels = []
y = 0  # running y position tracker

# ── Row 0: GreenKube Impact Command Center ────────────────────────────────
panels.append({**row(100, "GreenKube Impact Command Center", collapsed=False, y=y)})
y += 1

# Radar-style sustainability score panel on the left.
panels.append(sustainability_radar(101, gridpos={"x": 0, "y": y, "w": 8, "h": 12}))

_scope2_expr = (
    "sum(max by (cluster, namespace) "
    f'(greenkube_dashboard_summary_co2e_grams_total{{{CLUSTER_FILTER}, window="$dashboard_window", '
    'namespace=~"$namespace", namespace!="__all__", scope="scope2"})) '
    "or "
    f"sum(max by (cluster) (greenkube_cluster_co2e_grams_total{{{CLUSTER_FILTER}}}))"
)
_scope3_expr = (
    "sum(max by (cluster, namespace) "
    f'(greenkube_dashboard_summary_co2e_grams_total{{{CLUSTER_FILTER}, window="$dashboard_window", '
    'namespace=~"$namespace", namespace!="__all__", scope="scope3"})) '
    "or "
    f"sum(max by (cluster) (greenkube_cluster_embodied_co2e_grams_total{{{CLUSTER_FILTER}}}))"
)
_cloud_cost_expr = (
    "sum(max by (cluster, namespace) "
    f'(greenkube_dashboard_summary_cost_dollars_total{{{CLUSTER_FILTER}, window="$dashboard_window", '
    'namespace=~"$namespace", namespace!="__all__"})) '
    "or "
    f"sum(max by (cluster) (greenkube_cluster_cost_dollars_total{{{CLUSTER_FILTER}}}))"
)
_co2_saved_expr = (
    "sum(max by (cluster, namespace) "
    f'(greenkube_dashboard_savings_co2e_grams_total{{{CLUSTER_FILTER}, window="$dashboard_window", '
    'namespace=~"$namespace", namespace!="__all__", recommendation_type="all"}))'
)
_cost_saved_expr = (
    "sum(max by (cluster, namespace) "
    f'(greenkube_dashboard_savings_cost_dollars_total{{{CLUSTER_FILTER}, window="$dashboard_window", '
    'namespace=~"$namespace", namespace!="__all__", recommendation_type="all"}))'
)
_implemented_expr = (
    "sum(max by (cluster, namespace, type) "
    f"(greenkube_recommendations_implemented_total{{{CLUSTER_FILTER}, "
    'namespace=~"$namespace", namespace!="__all__"}))'
)

panels.append(
    echarts_panel(
        102,
        "Footprint & Cost Mix",
        [
            {"expr": _scope2_expr, "legend": "Scope 2", "refId": "A"},
            {"expr": _scope3_expr, "legend": "Scope 3", "refId": "B"},
            {"expr": _cloud_cost_expr, "legend": "Cloud cost", "refId": "C"},
        ],
        ECHARTS_FOOTPRINT_MIX_OPTION,
        gridpos={"x": 8, "y": y, "w": 8, "h": 6},
        description="Grouped ECharts bar chart for operational CO₂e, embodied CO₂e, and cloud cost.",
    )
)
panels.append(
    echarts_panel(
        103,
        "GreenKube Impact",
        [
            {"expr": _co2_saved_expr, "legend": "CO₂e avoided", "refId": "A"},
            {"expr": _cost_saved_expr, "legend": "Cost avoided", "refId": "B"},
            {
                "expr": _implemented_expr,
                "legend": "Implemented",
                "refId": "C",
            },
        ],
        ECHARTS_IMPACT_LEDGER_OPTION,
        gridpos={"x": 16, "y": y, "w": 8, "h": 6},
        description="Grouped ECharts impact strip for realized savings and implemented recommendations.",
    )
)

# Top 3 priority groups — instant queries so topk returns exactly 3 in sorted order.
# sort_desc() ensures descending order (highest bar at top) in the Prometheus response.
_top3_co2e_expr = (
    "sort_desc(topk(3, sum by (namespace) "
    "(max by (cluster, namespace) "
    f'(greenkube_dashboard_summary_co2e_grams_total{{{CLUSTER_FILTER}, window="$dashboard_window", '
    'namespace=~"$namespace", namespace!="__all__", scope="all"}))))'
)
_top3_cost_expr = (
    "sort_desc(topk(3, sum by (namespace) "
    "(max by (cluster, namespace) "
    f'(greenkube_dashboard_summary_cost_dollars_total{{{CLUSTER_FILTER}, window="$dashboard_window", '
    'namespace=~"$namespace", namespace!="__all__"}))))'
)
_top3_types_expr = (
    f"topk(3, sum by (type)"
    f" (max by (cluster, namespace, type, priority)"
    f' (greenkube_recommendations_total{{{CLUSTER_FILTER}, namespace=~"$namespace", namespace!="__all__"}})))'
)
panels.append(
    echarts_panel(
        104,
        "Action Priorities",
        [
            {"expr": _top3_co2e_expr, "legend": "{{namespace}}", "refId": "A", "range": False, "instant": True},
            {"expr": _top3_cost_expr, "legend": "{{namespace}}", "refId": "B", "range": False, "instant": True},
            {"expr": _top3_types_expr, "legend": "{{type}}", "refId": "C", "range": False, "instant": True},
        ],
        ECHARTS_ACTION_PRIORITIES_OPTION,
        gridpos={"x": 8, "y": y + 6, "w": 16, "h": 6},
        description="Grouped ECharts action panel for the top namespaces and recommendation types.",
    )
)
y += 12

# ── Regional node map ─────────────────────────────────────────────────────
panels.append({**row(130, "Regional Node Cleanliness", collapsed=True, y=y)})
y += 1

# Use only the cluster filter so all nodes always appear on the map regardless
# of the $node variable selection. The lookup remains the EM zone used for grid
# intensity, while the node_info join adds Kubernetes topology labels.
_node_effective_intensity_expr = (
    "avg by (cluster, zone, lookup, nodes, node_count, bubble_size, bubble_label, map_label) "
    f"(greenkube_zone_grid_intensity_gco2_kwh{{{CLUSTER_FILTER}}})"
)
panels.append(
    geomap(
        131,
        "Node Region Cleanliness Map",
        [
            {
                "expr": _node_effective_intensity_expr,
                "legend": "{{node}} · {{zone}} · {{region}} grid intensity",
                "refId": "A",
            },
        ],
        unit="gCO₂/kWh",
        gridpos={"x": 0, "y": y, "w": 24, "h": 9},
    )
)
y += 9

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
                    f' (greenkube_recommendations_total{{{CLUSTER_FILTER}, namespace!="__all__"}}))'
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
            f"sum(max by (cluster, namespace, type, priority) "
            f'(greenkube_recommendations_total{{{CLUSTER_FILTER}, namespace=~"$namespace", namespace!="__all__"}}))',
            "short",
            False,
        ),
        (
            "CO₂e Avoided (selected window)",
            _co2_saved_expr,
            "g CO₂e",
            True,
        ),
        (
            "Cost Avoided (selected window)",
            _cost_saved_expr,
            "currencyUSD",
            True,
        ),
        (
            "Recommendations Implemented",
            _implemented_expr,
            "short",
            False,
        ),
        (
            "Open Recommendations",
            f"sum(max by (cluster, namespace, type, priority) "
            f'(greenkube_recommendations_total{{{CLUSTER_FILTER}, namespace=~"$namespace", namespace!="__all__"}}))',
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
            f' (greenkube_recommendations_total{{{CLUSTER_FILTER}, namespace=~"$namespace", namespace!="__all__"}}))'
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
        {"type": "panel", "id": "volkovlabs-echarts-panel", "name": "Business Charts", "version": "7.2.2"},
        {"type": "panel", "id": "geomap", "name": "Geomap", "version": ""},
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
                "current": {"text": "prometheus", "value": PROMETHEUS_DEFAULT_UID},
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
                "query": 'label_values(greenkube_namespace_co2e_grams_total{cluster=~"$cluster"}, namespace)',
                "refresh": 2,
                "sort": 1,
                "multi": True,
                "includeAll": True,
                "allValue": ".*",
                "hide": 0,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "dashboard_window",
                "type": "custom",
                "label": "Reporting window",
                "query": (
                    "1h : 3600s, 6h : 21600s, 24h : 86400s, 7d : 604800s, 30d : 2592000s, YTD : ytd, 1y : 31536000s"
                ),
                "options": [
                    {"text": "1h", "value": "3600s", "selected": False},
                    {"text": "6h", "value": "21600s", "selected": False},
                    {"text": "24h", "value": "86400s", "selected": False},
                    {"text": "7d", "value": "604800s", "selected": True},
                    {"text": "30d", "value": "2592000s", "selected": False},
                    {"text": "YTD", "value": "ytd", "selected": False},
                    {"text": "1y", "value": "31536000s", "selected": False},
                ],
                "current": {"text": "7d", "value": "604800s"},
                "hide": 0,
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
