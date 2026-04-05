/**
 * Tests for the ECharts option builder functions.
 *
 * Validates chart structure, axis configuration, series generation,
 * and edge cases (empty data, single entry, aggregation logic).
 */
import { describe, it, expect } from 'vitest';
import {
	buildTimeseriesOption,
	buildMultiSeriesOption,
	buildNamespaceDonutOption,
	buildTopPodsOption
} from '$lib/charts.js';


// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------
const TIMESERIES_DATA = [
	{ timestamp: '2026-04-01T00:00:00Z', co2e_grams: 10, total_cost: 0.5, joules: 3000 },
	{ timestamp: '2026-04-01T01:00:00Z', co2e_grams: 15, total_cost: 0.7, joules: 4500 },
	{ timestamp: '2026-04-01T02:00:00Z', co2e_grams: 12, total_cost: 0.6, joules: 3600 }
];

const METRICS_DATA = [
	{ namespace: 'prod', pod_name: 'api-1', co2e_grams: 50, total_cost: 1.0 },
	{ namespace: 'prod', pod_name: 'api-2', co2e_grams: 30, total_cost: 0.8 },
	{ namespace: 'staging', pod_name: 'web-1', co2e_grams: 20, total_cost: 0.5 },
	{ namespace: 'monitoring', pod_name: 'prom-1', co2e_grams: 10, total_cost: 0.3 }
];


// ---------------------------------------------------------------------------
// buildTimeseriesOption
// ---------------------------------------------------------------------------
describe('buildTimeseriesOption', () => {
	it('returns base option for empty data', () => {
		const result = buildTimeseriesOption([]);
		expect(result).toBeDefined();
		expect(result.series).toBeUndefined();
	});

	it('returns base option for null data', () => {
		const result = buildTimeseriesOption(null);
		expect(result).toBeDefined();
	});

	it('builds correct xAxis categories from timestamps', () => {
		const result = buildTimeseriesOption(TIMESERIES_DATA);
		expect(result.xAxis.data).toEqual(TIMESERIES_DATA.map(d => d.timestamp));
	});

	it('builds a single CO2e series', () => {
		const result = buildTimeseriesOption(TIMESERIES_DATA);
		expect(result.series).toHaveLength(1);
		expect(result.series[0].name).toBe('CO₂e');
		expect(result.series[0].type).toBe('line');
	});

	it('maps correct data values', () => {
		const result = buildTimeseriesOption(TIMESERIES_DATA);
		expect(result.series[0].data).toEqual([10, 15, 12]);
	});

	it('includes area style gradient', () => {
		const result = buildTimeseriesOption(TIMESERIES_DATA);
		expect(result.series[0].areaStyle).toBeDefined();
		expect(result.series[0].areaStyle.color.type).toBe('linear');
	});

	it('accepts optional title', () => {
		const result = buildTimeseriesOption(TIMESERIES_DATA, { title: 'My Chart' });
		expect(result.title.text).toBe('My Chart');
	});

	it('accepts optional yAxisName', () => {
		const result = buildTimeseriesOption(TIMESERIES_DATA, { yAxisName: 'grams' });
		expect(result.yAxis.name).toBe('grams');
	});
});


// ---------------------------------------------------------------------------
// buildMultiSeriesOption
// ---------------------------------------------------------------------------
describe('buildMultiSeriesOption', () => {
	it('returns base option for empty data', () => {
		const result = buildMultiSeriesOption([]);
		expect(result).toBeDefined();
		expect(result.series).toBeUndefined();
	});

	it('returns base option for null', () => {
		const result = buildMultiSeriesOption(null);
		expect(result).toBeDefined();
	});

	it('builds three series (CO2e, Cost, Energy)', () => {
		const result = buildMultiSeriesOption(TIMESERIES_DATA);
		expect(result.series).toHaveLength(3);
		expect(result.series[0].name).toBe('CO₂e (g)');
		expect(result.series[1].name).toBe('Cost ($)');
		expect(result.series[2].name).toBe('Energy (kJ)');
	});

	it('converts joules to kJ in the energy series', () => {
		const result = buildMultiSeriesOption(TIMESERIES_DATA);
		expect(result.series[2].data).toEqual([3, 4.5, 3.6]);
	});

	it('has two yAxis (left and right)', () => {
		const result = buildMultiSeriesOption(TIMESERIES_DATA);
		expect(result.yAxis).toHaveLength(2);
	});

	it('includes a legend with all three series names', () => {
		const result = buildMultiSeriesOption(TIMESERIES_DATA);
		expect(result.legend.data).toEqual(['CO₂e (g)', 'Cost ($)', 'Energy (kJ)']);
	});
});


// ---------------------------------------------------------------------------
// buildNamespaceDonutOption
// ---------------------------------------------------------------------------
describe('buildNamespaceDonutOption', () => {
	it('groups metrics by namespace', () => {
		const result = buildNamespaceDonutOption(METRICS_DATA);
		const pieData = result.series[0].data;
		const names = pieData.map(d => d.name);
		expect(names).toContain('prod');
		expect(names).toContain('staging');
		expect(names).toContain('monitoring');
	});

	it('sums co2e_grams by namespace', () => {
		const result = buildNamespaceDonutOption(METRICS_DATA);
		const pieData = result.series[0].data;
		const prod = pieData.find(d => d.name === 'prod');
		expect(prod.value).toBe(80); // 50 + 30
	});

	it('sorts namespaces by value descending', () => {
		const result = buildNamespaceDonutOption(METRICS_DATA);
		const values = result.series[0].data.map(d => d.value);
		for (let i = 0; i < values.length - 1; i++) {
			expect(values[i]).toBeGreaterThanOrEqual(values[i + 1]);
		}
	});

	it('supports custom field', () => {
		const result = buildNamespaceDonutOption(METRICS_DATA, { field: 'total_cost' });
		const prodData = result.series[0].data.find(d => d.name === 'prod');
		expect(prodData.value).toBe(1.8); // 1.0 + 0.8
	});

	it('renders as pie chart type', () => {
		const result = buildNamespaceDonutOption(METRICS_DATA);
		expect(result.series[0].type).toBe('pie');
	});

	it('handles empty metrics', () => {
		const result = buildNamespaceDonutOption([]);
		expect(result.series[0].data).toEqual([]);
	});
});


// ---------------------------------------------------------------------------
// buildTopPodsOption
// ---------------------------------------------------------------------------
describe('buildTopPodsOption', () => {
	it('groups metrics by namespace/pod_name key', () => {
		const result = buildTopPodsOption(METRICS_DATA);
		const names = result.yAxis.data;
		expect(names).toContain('prod/api-1');
		expect(names).toContain('staging/web-1');
	});

	it('sorts by value descending and reverses for horizontal bar', () => {
		const result = buildTopPodsOption(METRICS_DATA);
		const values = result.series[0].data;
		// The values are reversed because horizontal bar charts display bottom-up
		// So the last value should be the largest
		expect(values[values.length - 1]).toBeGreaterThanOrEqual(values[0]);
	});

	it('limits to top N pods', () => {
		const result = buildTopPodsOption(METRICS_DATA, { limit: 2 });
		expect(result.yAxis.data).toHaveLength(2);
		expect(result.series[0].data).toHaveLength(2);
	});

	it('supports custom field', () => {
		const result = buildTopPodsOption(METRICS_DATA, { field: 'total_cost' });
		// api-1 has cost 1.0, api-2 has cost 0.8 → api-1 should be in top
		const names = result.yAxis.data;
		expect(names).toContain('prod/api-1');
	});

	it('renders as bar chart type', () => {
		const result = buildTopPodsOption(METRICS_DATA);
		expect(result.series[0].type).toBe('bar');
	});

	it('handles empty metrics', () => {
		const result = buildTopPodsOption([]);
		expect(result.yAxis.data).toEqual([]);
		expect(result.series[0].data).toEqual([]);
	});
});
