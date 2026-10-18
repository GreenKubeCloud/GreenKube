import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import DashboardPage from '../../src/routes/+page.svelte';
import * as api from '$lib/api.js';

vi.mock('$lib/api.js', () => ({
	getDashboardSummary: vi.fn(),
	getDashboardTimeseries: vi.fn(),
	getMetricsSummary: vi.fn(),
	getTimeseries: vi.fn(),
	getMetricsByNamespace: vi.fn(),
	getTopPods: vi.fn(),
	getActiveRecommendations: vi.fn(),
	getRecommendationSavings: vi.fn(),
	refreshDashboardSummary: vi.fn(),
	getNamespaces: vi.fn()
}));

vi.mock('echarts', () => ({
	init: () => ({
		setOption: vi.fn(),
		resize: vi.fn(),
		dispose: vi.fn()
	})
}));

const summary = {
	total_co2e_grams: 0,
	total_embodied_co2e_grams: 0,
	total_cost: 0,
	total_energy_joules: 0
};

const point = {
	bucket_ts: '2026-10-22T10:00:00Z',
	co2e_grams: 0,
	embodied_co2e_grams: 0,
	total_cost: 0,
	joules: 0
};

describe('DashboardPage', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.getDashboardSummary.mockResolvedValue({ windows: { '24h': summary } });
		api.getDashboardTimeseries.mockResolvedValue({ points: [point] });
		api.getMetricsSummary.mockResolvedValue(summary);
		api.getTimeseries.mockResolvedValue([point]);
		api.getMetricsByNamespace.mockResolvedValue([]);
		api.getTopPods.mockResolvedValue([]);
		api.getActiveRecommendations.mockResolvedValue([]);
		api.getRecommendationSavings.mockResolvedValue({});
		api.getNamespaces.mockResolvedValue([]);
	});

	it('aborts the active dashboard request when the page is unmounted', async () => {
		const { unmount } = render(DashboardPage);
		await waitFor(() => expect(api.getDashboardSummary).toHaveBeenCalled());

		const signal = api.getDashboardSummary.mock.calls.at(-1)[0].signal;
		expect(signal).toBeTruthy();

		unmount();

		expect(signal.aborted).toBe(true);
	});
});
