import { timeRanges } from '$lib/stores.js';

export const reportTimeRanges = timeRanges;

export const aggregationLevels = [
	{ value: 'raw', label: 'Raw data' },
	{ value: 'hourly', label: 'Hourly' },
	{ value: 'daily', label: 'Daily' },
	{ value: 'weekly', label: 'Weekly' },
	{ value: 'monthly', label: 'Monthly' },
	{ value: 'yearly', label: 'Yearly' }
];

export function getAggregationLevel(value) {
	return aggregationLevels.find((level) => level.value === value) ?? aggregationLevels[0];
}

export function buildReportRequestParams({ namespace, last, aggregationLevel } = {}) {
	const level = getAggregationLevel(aggregationLevel);
	const aggregate = level.value !== 'raw';

	return {
		namespace: namespace || undefined,
		last,
		aggregate,
		granularity: aggregate ? level.value : undefined
	};
}
