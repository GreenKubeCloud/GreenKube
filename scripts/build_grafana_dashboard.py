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


ECHARTS_ACTIONABLE_RECOMMENDATIONS_OPTION = (
    ECHARTS_THEME
    + """
const rawLimit = context.grafana && context.grafana.replaceVariables
    ? context.grafana.replaceVariables('$recommendation_limit')
    : '5';
const rawMetric = context.grafana && context.grafana.replaceVariables
    ? context.grafana.replaceVariables('$recommendation_metric')
    : 'co2';
const limit = Math.max(1, Math.min(10, Number(String(rawLimit).replace(/[^0-9]/g, '')) || 5));
const recommendationMetric = rawMetric === 'cost' ? 'cost' : 'co2';

const rowsByKey = new Map();

const upsertRow = (series, valueKey) => {
    const numberField = series?.fields.find((field) => field.type === 'number');
    const labels = numberField?.labels || {};
    const rank = Number(labels.rank || 0);
    if (!rank) {
        return;
    }

    const key = [labels.sort_metric, labels.rank, labels.namespace, labels.type, labels.resource].join('|');
    const existing = rowsByKey.get(key) || {
        rank,
        sortMetric: labels.sort_metric || recommendationMetric,
        namespace: labels.namespace || '_cluster',
        type: labels.type || 'UNKNOWN',
        resource: labels.resource || '_cluster',
        scope: labels.scope || 'pod',
        priority: labels.priority || 'medium',
        co2: 0,
        cost: 0,
    };
    existing[valueKey] = lastNumber(series);
    rowsByKey.set(key, existing);
};

context.panel.data.series.filter((series) => matchesRefId(series, 'A')).forEach((series) => upsertRow(series, 'co2'));
context.panel.data.series.filter((series) => matchesRefId(series, 'B')).forEach((series) => upsertRow(series, 'cost'));

const rows = Array.from(rowsByKey.values())
    .filter((row) => row.sortMetric === recommendationMetric)
    .filter((row) => row.co2 > 0 || row.cost > 0)
    .sort((a, b) => a.rank - b.rank)
    .slice(0, limit);

const primaryValue = (row) => recommendationMetric === 'cost' ? row.cost : row.co2;
const primaryUnit = recommendationMetric === 'cost' ? 'currencyUSD' : 'g CO₂e';
const maxPrimary = Math.max(1, ...rows.map(primaryValue));

if (!rows.length) {
    return {
        backgroundColor: 'transparent',
        graphic: [{
            type: 'text',
            left: 'center',
            top: 'middle',
            style: {
                text: 'No actionable recommendations with projected savings',
                fill: palette.muted,
                fontSize: 13,
                fontWeight: 600,
                textAlign: 'center',
                textVerticalAlign: 'middle',
            },
        }],
    };
}

return {
    backgroundColor: 'transparent',
    tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 23, 42, 0.96)',
        borderColor: palette.cyan,
        borderWidth: 1,
        textStyle: { color: palette.text },
        formatter: (params) => {
            const row = params.data;
            return [
                `<strong>#${row.rank} ${row.type.replace(/_/g, ' ')}</strong>`,
                `${row.namespace} / ${row.resource}`,
                `CO₂e: ${formatValue(row.co2, 'g CO₂e')}`,
                `Cost: ${formatValue(row.cost, 'currencyUSD')}`,
                `Ranked by ${recommendationMetric === 'cost' ? 'cost' : 'CO₂e'}`,
            ].join('<br/>');
        },
    },
    title: {
        text: 'Projected annual savings',
        subtext: `Top ${rows.length} ranked by ${recommendationMetric === 'cost' ? 'cost' : 'CO₂e'}`,
        left: 12,
        top: 4,
        textStyle: { color: palette.text, fontSize: 13, fontWeight: 800 },
        subtextStyle: { color: palette.muted, fontSize: 11, fontWeight: 600 },
    },
    grid: { top: 48, right: 12, bottom: 12, left: 12 },
    xAxis: { type: 'value', min: 0, max: 1, show: false },
    yAxis: { type: 'category', data: rows.map((row) => String(row.rank)), show: false },
    series: [{
        type: 'custom',
        coordinateSystem: 'cartesian2d',
        data: rows.map((row, index) => ({ ...row, value: [primaryValue(row), index] })),
        renderItem: (params) => {
            const row = rows[params.dataIndex];
            const coord = params.coordSys;
            const rowStep = coord.height / Math.max(rows.length, 1);
            const cardHeight = Math.max(42, Math.min(58, rowStep - 8));
            const x = coord.x;
            const y = coord.y + params.dataIndex * rowStep + (rowStep - cardHeight) / 2;
            const width = coord.width;
            const barX = x + Math.min(430, width * 0.48);
            const metricX = Math.max(barX + 124, x + width - 228);
            const barWidth = Math.max(6, Math.min(width - (barX - x) - 250, 220) * (primaryValue(row) / maxPrimary));
            const typeText = row.type.replace(/_/g, ' ');
            const targetText = `${row.namespace} / ${row.resource}`;

            return {
                type: 'group',
                children: [
                    {
                        type: 'rect',
                        shape: { x, y, width, height: cardHeight, r: 8 },
                        style: {
                            fill: params.dataIndex % 2 === 0 ? palette.panel : palette.panelSoft,
                            stroke: 'rgba(148, 163, 184, 0.18)',
                            lineWidth: 1,
                        },
                    },
                    {
                        type: 'rect',
                        shape: { x: x + 12, y: y + 13, width: 34, height: 22, r: 11 },
                        style: { fill: 'rgba(0, 255, 212, 0.14)', stroke: palette.cyan, lineWidth: 1 },
                    },
                    {
                        type: 'text',
                        style: {
                            x: x + 29,
                            y: y + 24,
                            text: `#${row.rank}`,
                            fill: palette.cyan,
                            fontSize: 12,
                            fontWeight: 800,
                            textAlign: 'center',
                            textVerticalAlign: 'middle',
                        },
                    },
                    {
                        type: 'text',
                        style: {
                            x: x + 58,
                            y: y + 17,
                            text: typeText,
                            fill: palette.text,
                            fontSize: 12,
                            fontWeight: 800,
                            width: Math.max(160, barX - x - 82),
                            overflow: 'truncate',
                        },
                    },
                    {
                        type: 'text',
                        style: {
                            x: x + 58,
                            y: y + 36,
                            text: targetText,
                            fill: palette.muted,
                            fontSize: 11,
                            fontWeight: 600,
                            width: Math.max(160, barX - x - 82),
                            overflow: 'truncate',
                        },
                    },
                    {
                        type: 'rect',
                        shape: { x: barX, y: y + 22, width: Math.min(220, width - (barX - x) - 250), height: 8, r: 4 },
                        style: { fill: 'rgba(148, 163, 184, 0.16)' },
                    },
                    {
                        type: 'rect',
                        shape: { x: barX, y: y + 22, width: barWidth, height: 8, r: 4 },
                        style: {
                            fill: recommendationMetric === 'cost' ? palette.green : palette.cyan,
                            shadowBlur: 12,
                            shadowColor: recommendationMetric === 'cost'
                                ? 'rgba(52, 211, 153, 0.35)'
                                : 'rgba(0, 255, 212, 0.35)',
                        },
                    },
                    {
                        type: 'text',
                        style: {
                            x: metricX,
                            y: y + 18,
                            text: formatValue(row.co2, 'g CO₂e'),
                            fill: palette.cyan,
                            fontSize: 12,
                            fontWeight: 800,
                            width: 106,
                            overflow: 'truncate',
                        },
                    },
                    {
                        type: 'text',
                        style: {
                            x: metricX + 116,
                            y: y + 18,
                            text: formatValue(row.cost, 'currencyUSD'),
                            fill: palette.green,
                            fontSize: 12,
                            fontWeight: 800,
                            width: 86,
                            overflow: 'truncate',
                        },
                    },
                    {
                        type: 'text',
                        style: {
                            x: barX,
                            y: y + 39,
                            text: formatValue(primaryValue(row), primaryUnit),
                            fill: palette.text,
                            fontSize: 11,
                            fontWeight: 700,
                        },
                    },
                ],
            };
        },
    }],
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
SCORE_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "#FB7185", "value": None},
        {"color": "#F59E0B", "value": 60},
        {"color": "#00FFD4", "value": 80},
    ],
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

# ── Row 1: Actionable Recommendations ─────────────────────────────────────
panels.append({**row(400, "Actionable Recommendations", collapsed=False, y=y)})
y += 1

_top_recommendations_co2_expr = (
    f'greenkube_top_recommendations{{{CLUSTER_FILTER}, namespace=~"$namespace", '
    'sort_metric="$recommendation_metric", value_metric="co2e_grams"}'
)
_top_recommendations_cost_expr = (
    f'greenkube_top_recommendations{{{CLUSTER_FILTER}, namespace=~"$namespace", '
    'sort_metric="$recommendation_metric", value_metric="cost_dollars"}'
)
panels.append(
    echarts_panel(
        401,
        "Top Actionable Recommendations",
        [
            {
                "expr": _top_recommendations_co2_expr,
                "legend": "#{{rank}} {{type}} {{namespace}}/{{resource}} CO₂e",
                "refId": "A",
                "range": False,
                "instant": True,
            },
            {
                "expr": _top_recommendations_cost_expr,
                "legend": "#{{rank}} {{type}} {{namespace}}/{{resource}} cost",
                "refId": "B",
                "range": False,
                "instant": True,
            },
        ],
        ECHARTS_ACTIONABLE_RECOMMENDATIONS_OPTION,
        gridpos={"x": 0, "y": y, "w": 24, "h": 12},
        description="Ranked action cards for the active recommendations with the largest projected annual savings.",
    )
)
y += 8

# ── Row 2: CO₂e and Cost by Namespace ─────────────────────────────────────────────
panels.append({**row(500, "CO₂e and Cost by Namespace", collapsed=False, y=y)})
y += 1

panels.append(
    piechart(
        501,
        "CO₂e by Namespace",
        f"sum by (namespace) (greenkube_namespace_co2e_grams_total{{{CLUSTER_FILTER}}})",
        "{{namespace}}",
        unit="g CO₂e",
        gridpos={"x": 0, "y": y, "w": 12, "h": 8},
    )
)
panels.append(
    piechart(
        502,
        "Cost by Namespace",
        f"sum by (namespace) (greenkube_namespace_cost_dollars_total{{{CLUSTER_FILTER}}})",
        "{{namespace}}",
        unit="currencyUSD",
        gridpos={"x": 12, "y": y, "w": 12, "h": 8},
    )
)
y += 8

# ── Regional node map ─────────────────────────────────────────────────────
panels.append({**row(130, "Regional Node Cleanliness", collapsed=True, y=y)})
y += 1

# Use only the cluster filter so all nodes always appear on the map. The lookup remains the EM zone used for grid
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

# ── Row 3: Top Emitters & Spenders ───────────────────────────────────────
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
            "Top 15 Pods — Cost",
            f"topk(15, max by (namespace, pod) (greenkube_pod_cost_dollars{{{NS_FILTER}}}))",
            "currencyUSD",
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
            gridpos={"x": i * 12, "y": y, "w": 12, "h": 10},
        )
    )
y += 10

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
        {"type": "panel", "id": "piechart", "name": "Pie chart", "version": ""},
        {"type": "panel", "id": "bargauge", "name": "Bar gauge", "version": ""},
        {"type": "panel", "id": "volkovlabs-echarts-panel", "name": "Business Charts", "version": "7.2.2"},
        {"type": "panel", "id": "geomap", "name": "Geomap", "version": ""},
    ],
    "id": None,
    "uid": "greenkube-fingreenops",
    "title": "GreenKube FinGreenOps Dashboard",
    "description": (
        "FinGreenOps dashboard for Kubernetes — sustainability score, CO₂ emissions (Scope 2 & 3), "
        "cloud costs, regional node cleanliness, top namespace impact, pod spend, and realized savings."
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
                "name": "recommendation_metric",
                "type": "custom",
                "label": "Recommendation ranking",
                "query": "CO₂e : co2, Cost : cost",
                "options": [
                    {"text": "CO₂e", "value": "co2", "selected": True},
                    {"text": "Cost", "value": "cost", "selected": False},
                ],
                "current": {"text": "CO₂e", "value": "co2"},
                "hide": 0,
            },
            {
                "name": "recommendation_limit",
                "type": "custom",
                "label": "Recommendation count",
                "query": "3, 5, 10",
                "options": [
                    {"text": "3", "value": "3", "selected": False},
                    {"text": "5", "value": "5", "selected": True},
                    {"text": "10", "value": "10", "selected": False},
                ],
                "current": {"text": "5", "value": "5"},
                "hide": 0,
            },
        ]
    },
    "annotations": {"list": []},
    "panels": panels,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(DASHBOARD, indent=2, ensure_ascii=False))
    print(f"Written {len(panels)} panels to {OUT}")
