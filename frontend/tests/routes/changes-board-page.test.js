/**
 * /changes pipeline board (K4): columns, rows, links, filters and states.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const pageUrl = vi.hoisted(() => ({ href: 'http://localhost:3000/changes' }));

vi.mock('$app/state', () => ({
	page: {
		get url() {
			return new URL(pageUrl.href);
		},
		params: {},
		route: { id: '/changes' },
		status: 200,
		state: {}
	}
}));

vi.mock('#lib/api.js', () => {
	class ApiError extends Error {
		constructor(message, status) {
			super(message);
			this.name = 'ApiError';
			this.status = status;
		}
	}
	return {
		ApiError,
		getChange: vi.fn(),
		listChanges: vi.fn(),
		getOpenPullRequests: vi.fn(),
		getRecommendationPullRequests: vi.fn(),
		listDeliveringRecommendations: vi.fn(),
		getAutomationPlanStatus: vi.fn()
	};
});

import * as api from '#lib/api.js';
import ChangesPage from '../../src/routes/changes/+page.svelte';

const ago = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString();

function seed() {
	api.listChanges.mockResolvedValue({
		items: [
			{
				change_id: 'chg-1',
				plan_id: 'plan-1',
				state: 'measuring',
				target: { kind: 'Deployment', namespace: 'shop', name: 'api' },
				recommendation_ids: [11],
				pr_url: 'https://github.com/acme/gitops/pull/1',
				state_updated_at: ago(5)
			},
			{
				change_id: 'chg-0',
				plan_id: 'plan-0',
				state: 'regressed',
				target: { kind: 'Deployment', namespace: 'shop', name: 'web' },
				recommendation_ids: [10],
				state_updated_at: ago(60)
			}
		],
		next_cursor: null
	});
	api.getOpenPullRequests.mockResolvedValue([
		{ id: 2, recommendation_id: 12, plan_id: 'plan-2', status: 'open', repo: 'acme/gitops', pr_number: 2, pr_url: 'https://github.com/acme/gitops/pull/2', created_at: ago(20) }
	]);
	api.listDeliveringRecommendations.mockResolvedValue({
		items: [
			{ id: 12, target: { kind: 'Deployment', namespace: 'payments', name: 'checkout' } },
			{ id: 13, target: { kind: 'CronJob', namespace: 'batch', name: 'report' } }
		]
	});
	api.getRecommendationPullRequests.mockResolvedValue([
		{ id: 3, recommendation_id: 13, plan_id: 'plan-3', status: 'merged', sync_status: 'pending', repo: 'acme/gitops', updated_at: ago(10) }
	]);
	api.getAutomationPlanStatus.mockImplementation(async (id) => ({
		plan_id: id,
		status: id === 'plan-3' ? 'merged' : 'pr_open',
		recommendation_ids: id === 'plan-3' ? [13] : [12]
	}));
}

describe('/changes board', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		pageUrl.href = 'http://localhost:3000/changes';
		seed();
	});

	it('shows one pipeline from plan to outcome with every change in its column', async () => {
		render(ChangesPage);

		const board = await screen.findByTestId('change-board');
		const headings = within(board).getAllByRole('heading', { level: 2 }).map((node) => node.textContent.replace(/\s+/g, ' ').trim());
		expect(headings).toEqual(['Plan 0 changes', 'PR open 1 change', 'Merged 1 change', 'Synced 0 changes', 'Applied 0 changes', 'Measuring 1 change', 'Outcome 1 change']);

		const measuring = screen.getByTestId('stage-measuring');
		expect(within(measuring).getByRole('link', { name: 'Deployment/api' })).toHaveAttribute('href', '/changes/plan-1');
		expect(within(measuring).getByRole('link', { name: 'Recommendation #11' })).toHaveAttribute('href', '/recommendations/11');
		expect(within(measuring).getByRole('link', { name: 'Ledger' })).toHaveAttribute('href', '/ledger?change=chg-1');
		expect(within(measuring).getByRole('link', { name: /PR/ })).toHaveAttribute('href', 'https://github.com/acme/gitops/pull/1');

		const open = screen.getByTestId('stage-pr_open');
		expect(within(open).getByRole('link', { name: 'Deployment/checkout' })).toHaveAttribute('href', '/changes/plan-2');
		expect(within(open).getByRole('link', { name: /PR #2/ })).toHaveAttribute('target', '_blank');
		expect(within(open).queryByRole('link', { name: 'Ledger' })).toBeNull();

		const merged = screen.getByTestId('stage-merged');
		expect(merged).toHaveTextContent('CronJob/report');
		expect(merged).toHaveTextContent('Sync pending');

		const outcome = screen.getByTestId('stage-outcome');
		expect(outcome).toHaveTextContent('Regressed (rollback review)');
		expect(within(outcome).getByTestId('row-attention')).toHaveTextContent('Regressed: review the rollback');
	});

	it('filters by stage, namespace, attention and text', async () => {
		render(ChangesPage);
		await screen.findByTestId('change-board');

		await fireEvent.change(screen.getByTestId('filter-stage'), { target: { value: 'measuring' } });
		await waitFor(() => expect(screen.queryByTestId('stage-pr_open')).toBeNull());
		expect(screen.getAllByTestId('change-row')).toHaveLength(1);
		expect(window.location.search).toBe('?stage=measuring');

		await fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
		await waitFor(() => expect(screen.getAllByTestId('change-row')).toHaveLength(4));

		const namespaces = within(screen.getByTestId('filter-namespace')).getAllByRole('option').map((node) => node.textContent);
		expect(namespaces).toEqual(['All namespaces', 'batch', 'payments', 'shop']);
		await fireEvent.change(screen.getByTestId('filter-namespace'), { target: { value: 'shop' } });
		await waitFor(() => expect(screen.getAllByTestId('change-row')).toHaveLength(2));

		await fireEvent.click(screen.getByTestId('filter-attention'));
		await waitFor(() => expect(screen.getAllByTestId('change-row')).toHaveLength(1));
		expect(screen.getByTestId('change-row')).toHaveTextContent('Deployment/web');

		await fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
		await fireEvent.input(screen.getByTestId('filter-query'), { target: { value: 'checkout' } });
		await waitFor(() => expect(screen.getAllByTestId('change-row')).toHaveLength(1));
		await fireEvent.input(screen.getByTestId('filter-query'), { target: { value: 'nothing-matches' } });
		expect(await screen.findByTestId('board-no-match')).toBeInTheDocument();
	});

	it('opens with the filters of a shared link', async () => {
		pageUrl.href = 'http://localhost:3000/changes?stage=merged&q=report';
		render(ChangesPage);

		await screen.findByTestId('change-board');
		expect(screen.getByTestId('filter-stage')).toHaveValue('merged');
		expect(screen.getByTestId('filter-query')).toHaveValue('report');
		expect(screen.getAllByTestId('change-row')).toHaveLength(1);
		expect(screen.queryByTestId('stage-outcome')).toBeNull();
	});

	it('explains an empty pipeline', async () => {
		api.listChanges.mockResolvedValue({ items: [], next_cursor: null });
		api.getOpenPullRequests.mockResolvedValue([]);
		api.listDeliveringRecommendations.mockResolvedValue({ items: [] });
		render(ChangesPage);

		const empty = await screen.findByTestId('board-empty');
		expect(empty).toHaveTextContent('No change in flight');
		expect(within(empty).getByRole('link')).toHaveAttribute('href', '/recommendations');
	});

	it('reports a failed read but keeps what the others found', async () => {
		api.getOpenPullRequests.mockRejectedValue(new Error('Pull requests are unavailable.'));
		render(ChangesPage);

		expect(await screen.findByRole('alert')).toHaveTextContent('Pull requests are unavailable.');
		expect(screen.getByTestId('stage-measuring')).toHaveTextContent('Deployment/api');
	});

	it('shows one alert when every read fails with the same message', async () => {
		api.listChanges.mockRejectedValue(new TypeError('Failed to fetch'));
		api.getOpenPullRequests.mockRejectedValue(new TypeError('Failed to fetch'));
		api.listDeliveringRecommendations.mockRejectedValue(new TypeError('Failed to fetch'));
		render(ChangesPage);

		await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(1));
		expect(screen.getByRole('alert')).toHaveTextContent('Failed to fetch');
	});

	it('loads older changes with the cursor', async () => {
		api.listChanges
			.mockResolvedValueOnce({ items: [{ change_id: 'new', plan_id: 'plan-n', state: 'applied', state_updated_at: ago(1) }], next_cursor: 'c1' })
			.mockResolvedValueOnce({ items: [{ change_id: 'old', plan_id: 'plan-o', state: 'realized', state_updated_at: ago(900) }], next_cursor: null });
		render(ChangesPage);

		await fireEvent.click(await screen.findByTestId('load-older'));
		await waitFor(() => expect(screen.getByTestId('count-outcome')).toHaveTextContent('1'));
		expect(api.listChanges).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: 'c1' }));
		expect(screen.queryByTestId('load-older')).toBeNull();
	});
});
