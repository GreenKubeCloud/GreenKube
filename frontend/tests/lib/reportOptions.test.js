/**
 * Tests for report page option helpers.
 */
import { describe, it, expect } from 'vitest';
import {
	reportTimeRanges,
	aggregationLevels,
	groupByOptions,
	buildReportRequestParams
} from '$lib/reportOptions.js';


describe('reportTimeRanges', () => {
	it('uses the pre-computed dashboard window slugs including YTD', () => {
		expect(reportTimeRanges.map((range) => range.value)).toEqual([
			'1h',
			'6h',
			'24h',
			'7d',
			'30d',
			'1y',
			'ytd'
		]);
	});
});

describe('aggregationLevels', () => {
	it('offers raw plus explicit aggregation levels', () => {
		expect(aggregationLevels.map((level) => level.value)).toEqual([
			'raw',
			'hourly',
			'daily',
			'weekly',
			'monthly',
			'yearly'
		]);
	});
});

describe('groupByOptions', () => {
	it('offers pod and namespace report grouping', () => {
		expect(groupByOptions.map((option) => option.value)).toEqual(['pod', 'namespace']);
	});
});


describe('buildReportRequestParams', () => {
	it('keeps aggregation level independent from the selected window', () => {
		expect(buildReportRequestParams({ namespace: 'prod', last: '24h', aggregationLevel: 'monthly' })).toEqual({
			namespace: 'prod',
			last: '24h',
			aggregate: true,
			granularity: 'monthly',
			group_by: 'pod'
		});

		expect(buildReportRequestParams({ namespace: 'prod', last: 'ytd', aggregationLevel: 'hourly' })).toEqual({
			namespace: 'prod',
			last: 'ytd',
			aggregate: true,
			granularity: 'hourly',
			group_by: 'pod'
		});
	});

	it('omits namespace and granularity for raw reports', () => {
		expect(buildReportRequestParams({ namespace: '', last: '7d', aggregationLevel: 'raw' })).toEqual({
			namespace: undefined,
			last: '7d',
			aggregate: false,
			granularity: undefined
		});
	});

	it('builds params for selected report years', () => {
		expect(
			buildReportRequestParams({
				namespace: 'prod',
				timeMode: 'yearly',
				years: [2026, 2025],
				aggregationLevel: 'yearly',
				groupBy: 'namespace'
			})
		).toEqual({
			namespace: 'prod',
			years: [2026, 2025],
			aggregate: true,
			granularity: 'yearly',
			group_by: 'namespace'
		});
	});

	it('builds params for custom date ranges', () => {
		expect(
			buildReportRequestParams({
				timeMode: 'custom',
				start: '2025-01-01',
				end: '2025-01-31',
				aggregationLevel: 'raw'
			})
		).toEqual({
			namespace: undefined,
			start: '2025-01-01',
			end: '2025-01-31',
			aggregate: false,
			granularity: undefined
		});
	});
});
