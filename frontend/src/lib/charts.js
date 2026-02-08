/**
 * ECharts theme and option builders for GreenKube.
 */

const COLORS = {
	green: '#22c55e',
	greenLight: '#4ade80',
	greenDark: '#15803d',
	blue: '#3b82f6',
	blueLight: '#60a5fa',
	yellow: '#eab308',
	red: '#ef4444',
	purple: '#a855f7',
	cyan: '#06b6d4',
	orange: '#f97316',
	dark: {
		bg: 'transparent',
		text: '#94a3b8',
		border: '#334155',
		gridLine: '#1e293b'
	}
};

const BASE_OPTION = {
	backgroundColor: COLORS.dark.bg,
	textStyle: {
		color: COLORS.dark.text,
		fontFamily: 'Inter, system-ui, sans-serif'
	},
	grid: {
		left: '3%',
		right: '3%',
		bottom: '8%',
		top: '12%',
		containLabel: true
	},
	tooltip: {
		trigger: 'axis',
		backgroundColor: '#1e293b',
		borderColor: '#334155',
		textStyle: { color: '#e2e8f0', fontSize: 12 },
		axisPointer: {
			type: 'cross',
			crossStyle: { color: '#475569' }
		}
	}
};

/**
 * Build a time-series area chart option.
 */
export function buildTimeseriesOption(data, { title = '', yAxisName = '' } = {}) {
	if (!data || data.length === 0) return { ...BASE_OPTION };

	const timestamps = data.map(d => d.timestamp);

	return {
		...BASE_OPTION,
		title: title ? { text: title, textStyle: { color: '#e2e8f0', fontSize: 14, fontWeight: 500 }, left: '1%', top: '2%' } : undefined,
		xAxis: {
			type: 'category',
			data: timestamps,
			axisLine: { lineStyle: { color: COLORS.dark.border } },
			axisLabel: { color: COLORS.dark.text, fontSize: 10, rotate: 0, formatter: (v) => formatTimeLabel(v) },
			splitLine: { show: false }
		},
		yAxis: {
			type: 'value',
			name: yAxisName,
			nameTextStyle: { color: COLORS.dark.text, fontSize: 11 },
			axisLine: { show: false },
			axisLabel: { color: COLORS.dark.text, fontSize: 10 },
			splitLine: { lineStyle: { color: COLORS.dark.gridLine, type: 'dashed' } }
		},
		series: [
			{
				name: 'CO₂e',
				type: 'line',
				smooth: true,
				symbol: 'none',
				data: data.map(d => d.co2e_grams),
				lineStyle: { color: COLORS.green, width: 2 },
				areaStyle: {
					color: {
						type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
						colorStops: [
							{ offset: 0, color: 'rgba(34,197,94,0.3)' },
							{ offset: 1, color: 'rgba(34,197,94,0.02)' }
						]
					}
				},
				itemStyle: { color: COLORS.green }
			}
		]
	};
}

/**
 * Build a multi-series area chart (CO2, Cost, Energy).
 */
export function buildMultiSeriesOption(data) {
	if (!data || data.length === 0) return { ...BASE_OPTION };

	const timestamps = data.map(d => d.timestamp);

	return {
		...BASE_OPTION,
		legend: {
			data: ['CO₂e (g)', 'Cost ($)', 'Energy (kJ)'],
			textStyle: { color: COLORS.dark.text, fontSize: 11 },
			top: '2%',
			right: '3%'
		},
		xAxis: {
			type: 'category',
			data: timestamps,
			axisLine: { lineStyle: { color: COLORS.dark.border } },
			axisLabel: { color: COLORS.dark.text, fontSize: 10, formatter: (v) => formatTimeLabel(v) },
			splitLine: { show: false }
		},
		yAxis: [
			{
				type: 'value',
				name: 'CO₂e / Cost',
				nameTextStyle: { color: COLORS.dark.text, fontSize: 11 },
				axisLine: { show: false },
				axisLabel: { color: COLORS.dark.text, fontSize: 10 },
				splitLine: { lineStyle: { color: COLORS.dark.gridLine, type: 'dashed' } }
			},
			{
				type: 'value',
				name: 'Energy (kJ)',
				nameTextStyle: { color: COLORS.dark.text, fontSize: 11 },
				axisLine: { show: false },
				axisLabel: { color: COLORS.dark.text, fontSize: 10 },
				splitLine: { show: false }
			}
		],
		series: [
			{
				name: 'CO₂e (g)',
				type: 'line',
				smooth: true,
				symbol: 'none',
				data: data.map(d => d.co2e_grams),
				lineStyle: { color: COLORS.green, width: 2 },
				areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(34,197,94,0.2)' }, { offset: 1, color: 'rgba(34,197,94,0)' }] } },
				itemStyle: { color: COLORS.green }
			},
			{
				name: 'Cost ($)',
				type: 'line',
				smooth: true,
				symbol: 'none',
				data: data.map(d => d.total_cost),
				lineStyle: { color: COLORS.blue, width: 2 },
				areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(59,130,246,0.15)' }, { offset: 1, color: 'rgba(59,130,246,0)' }] } },
				itemStyle: { color: COLORS.blue }
			},
			{
				name: 'Energy (kJ)',
				type: 'bar',
				yAxisIndex: 1,
				data: data.map(d => d.joules / 1000),
				itemStyle: { color: 'rgba(234,179,8,0.4)', borderRadius: [2, 2, 0, 0] },
				barMaxWidth: 20
			}
		]
	};
}

/**
 * Build a namespace breakdown donut chart.
 */
export function buildNamespaceDonutOption(metrics, { field = 'co2e_grams', title = 'CO₂ by Namespace' } = {}) {
	const byNs = {};
	for (const m of metrics) {
		byNs[m.namespace] = (byNs[m.namespace] || 0) + (m[field] || 0);
	}
	const data = Object.entries(byNs)
		.map(([name, value]) => ({ name, value: +value.toFixed(4) }))
		.sort((a, b) => b.value - a.value);

	const palette = [COLORS.green, COLORS.blue, COLORS.yellow, COLORS.purple, COLORS.cyan, COLORS.orange, COLORS.red, COLORS.greenLight, COLORS.blueLight];

	return {
		...BASE_OPTION,
		tooltip: {
			trigger: 'item',
			backgroundColor: '#1e293b',
			borderColor: '#334155',
			textStyle: { color: '#e2e8f0', fontSize: 12 },
			formatter: '{b}: {c} ({d}%)'
		},
		legend: {
			orient: 'vertical',
			right: '5%',
			top: 'center',
			textStyle: { color: COLORS.dark.text, fontSize: 11 }
		},
		series: [{
			name: title,
			type: 'pie',
			radius: ['45%', '70%'],
			center: ['35%', '50%'],
			avoidLabelOverlap: true,
			itemStyle: { borderRadius: 6, borderColor: '#0f172a', borderWidth: 2 },
			label: { show: false },
			emphasis: {
				label: { show: true, fontSize: 13, fontWeight: 'bold', color: '#e2e8f0' },
				itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' }
			},
			data: data.map((d, i) => ({ ...d, itemStyle: { color: palette[i % palette.length] } }))
		}]
	};
}

/**
 * Build a horizontal bar chart for top pods.
 */
export function buildTopPodsOption(metrics, { field = 'co2e_grams', title = 'Top Pods by CO₂', limit = 10 } = {}) {
	const byPod = {};
	for (const m of metrics) {
		const key = `${m.namespace}/${m.pod_name}`;
		byPod[key] = (byPod[key] || 0) + (m[field] || 0);
	}
	const sorted = Object.entries(byPod)
		.sort((a, b) => b[1] - a[1])
		.slice(0, limit);

	const names = sorted.map(([k]) => k).reverse();
	const values = sorted.map(([, v]) => +v.toFixed(4)).reverse();

	return {
		...BASE_OPTION,
		grid: { left: '3%', right: '8%', bottom: '3%', top: '8%', containLabel: true },
		xAxis: {
			type: 'value',
			axisLine: { show: false },
			axisLabel: { color: COLORS.dark.text, fontSize: 10 },
			splitLine: { lineStyle: { color: COLORS.dark.gridLine, type: 'dashed' } }
		},
		yAxis: {
			type: 'category',
			data: names,
			axisLine: { lineStyle: { color: COLORS.dark.border } },
			axisLabel: { color: COLORS.dark.text, fontSize: 10, width: 180, overflow: 'truncate' }
		},
		series: [{
			type: 'bar',
			data: values,
			itemStyle: {
				color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: COLORS.greenDark }, { offset: 1, color: COLORS.green }] },
				borderRadius: [0, 4, 4, 0]
			},
			barMaxWidth: 18,
			label: { show: true, position: 'right', color: COLORS.dark.text, fontSize: 10 }
		}]
	};
}

function formatTimeLabel(v) {
	if (!v) return '';
	// "2026-02-08T12:00:00Z" -> "12:00"  or "Feb 8"
	if (v.includes('T')) {
		const d = new Date(v);
		if (isNaN(d.getTime())) return v;
		const hours = d.getUTCHours().toString().padStart(2, '0');
		const mins = d.getUTCMinutes().toString().padStart(2, '0');
		const day = d.getUTCDate();
		const month = d.toLocaleString('en', { month: 'short', timeZone: 'UTC' });
		// If time is 00:00, show date
		if (hours === '00' && mins === '00') return `${month} ${day}`;
		return `${hours}:${mins}`;
	}
	return v;
}
