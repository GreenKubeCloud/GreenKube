/**
 * Tests for report page option helpers.
 */
import { describe, it, expect } from 'vitest';
import {
	reportTimeRanges,
	aggregationLevels,
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


describe('buildReportRequestParams', () => {
	it('keeps aggregation level independent from the selected window', () => {
		expect(buildReportRequestParams({ namespace: 'prod', last: '24h', aggregationLevel: 'monthly' })).toEqual({
			namespace: 'prod',
			last: '24h',
			aggregate: true,
			granularity: 'monthly'
		});

		expect(buildReportRequestParams({ namespace: 'prod', last: 'ytd', aggregationLevel: 'hourly' })).toEqual({
			namespace: 'prod',
			last: 'ytd',
			aggregate: true,
			granularity: 'hourly'
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
});
