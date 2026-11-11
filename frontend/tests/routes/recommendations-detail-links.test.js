/**
 * Each recommendation card links to its detail page (K3).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import RecommendationsPage from '../../src/routes/recommendations/+page.svelte';
import * as api from '$lib/api.js';
import { resetOptimizationRunsSupport } from '$lib/optimizationRuns.js';

vi.mock('$lib/api.js', async (importOriginal) => {
	const original = await importOriginal();
	const mocked = {};
	for (const [name, value] of Object.entries(original)) {
		mocked[name] = typeof value === 'function' && name !== 'ApiError' ? vi.fn(() => Promise.resolve(undefined)) : value;
	}
	return mocked;
});

describe('recommendation cards', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		resetOptimizationRunsSupport();
		api.getNamespaces.mockResolvedValue([]);
		api.listOptimizationRuns.mockResolvedValue([]);
		api.getAppliedRecommendations.mockResolvedValue([]);
		api.getIgnoredRecommendations.mockResolvedValue([]);
		api.getOpenPullRequests.mockResolvedValue([]);
		api.getRecommendationSavings.mockResolvedValue({});
		api.getAutomationStatus.mockResolvedValue({ enabled: false, ready: false, mode: 'disabled' });
		api.getActiveRecommendations.mockResolvedValue([
			{ id: 101, type: 'RIGHTSIZING_CPU', namespace: 'production', pod_name: 'api-0', reason: 'CPU usage is consistently low' }
		]);
	});

	it('link to the recommendation detail page', async () => {
		render(RecommendationsPage);
		const link = await screen.findByTestId('recommendation-detail-link');
		expect(link).toHaveAttribute('href', '/recommendations/101');
		expect(link).toHaveTextContent('api-0');
	});
});
