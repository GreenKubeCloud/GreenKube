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

export const groupByOptions = [
	{ value: 'pod', label: 'Pod' },
	{ value: 'namespace', label: 'Namespace' }
];

export function getAggregationLevel(value) {
	return aggregationLevels.find((level) => level.value === value) ?? aggregationLevels[0];
}

export function getGroupBy(value) {
	return groupByOptions.find((option) => option.value === value) ?? groupByOptions[0];
}

export function buildReportRequestParams({
	namespace,
	last,
	timeMode = 'relative',
	years = [],
	start,
	end,
	aggregationLevel,
	groupBy = 'pod'
} = {}) {
	const level = getAggregationLevel(aggregationLevel);
	const aggregate = level.value !== 'raw';
	const params = {
		namespace: namespace || undefined
	};

	if (timeMode === 'yearly') {
		params.years = Array.isArray(years) ? years.filter(Boolean) : [];
	} else if (timeMode === 'custom') {
		params.start = start || undefined;
		params.end = end || undefined;
	} else {
		params.last = last;
	}

	params.aggregate = aggregate;
	params.granularity = aggregate ? level.value : undefined;
	if (aggregate) {
		params.group_by = getGroupBy(groupBy).value;
	}

	return params;
}
