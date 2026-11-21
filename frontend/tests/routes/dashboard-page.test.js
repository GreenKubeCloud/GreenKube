import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import DashboardPage from '../../src/routes/+page.svelte';
import * as api from '#lib/api.js';

vi.mock('#lib/api.js', () => ({
	getDashboardSummary: vi.fn(),
	getDashboardTimeseries: vi.fn(),
	getMetricsSummary: vi.fn(),
	getTimeseries: vi.fn(),
	getMetricsByNamespace: vi.fn(),
	getTopPods: vi.fn(),
	getActiveRecommendations: vi.fn(),
	getRecommendationSavings: vi.fn(),
	refreshDashboardSummary: vi.fn(),
	getNamespaces: vi.fn(),
	getOpenPullRequests: vi.fn(),
	listChanges: vi.fn()
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
		api.getOpenPullRequests.mockResolvedValue([]);
		api.listChanges.mockResolvedValue({ items: [], next_cursor: null });
	});

	it('leaves the footprint charts to /footprint and links there (K7)', async () => {
		const { findByTestId, container, queryByLabelText } = render(DashboardPage);

		const footprint = await findByTestId('footprint-link');
		expect(footprint.querySelector('a')).toHaveAttribute('href', '/footprint');
		await waitFor(() => expect(api.getActiveRecommendations).toHaveBeenCalled());
		expect(api.getDashboardSummary).not.toHaveBeenCalled();
		expect(api.getTopPods).not.toHaveBeenCalled();
		expect(container.textContent).not.toContain('CO₂ Emissions Over Time');
		// The period only filters footprint charts: the Overview shows the namespace alone.
		expect(queryByLabelText('Period')).toBeNull();
	});

	it('links the changes in flight to the changes board (K4)', async () => {
		const { findByTestId } = render(DashboardPage);
		const strip = await findByTestId('pipeline-strip');
		expect(strip.querySelector('a')).toHaveAttribute('href', '/changes');
	});

	it('reads recommendations without scheduling a scan and aborts them on unmount (BUG-25)', async () => {
		const { unmount } = render(DashboardPage);
		await waitFor(() => expect(api.getActiveRecommendations).toHaveBeenCalled());

		const [params] = api.getActiveRecommendations.mock.calls.at(-1);
		expect(params).not.toHaveProperty('refresh');
		const savingsSignal = api.getRecommendationSavings.mock.calls.at(-1)[0].signal;

		unmount();

		expect(params.signal.aborted).toBe(true);
		expect(savingsSignal.aborted).toBe(true);
	});

	it('shows the top recommendations in server order with their expected value (I1)', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			{ id: 1, type: 'RIGHTSIZING_CPU', pod_name: 'best', reason: 'r1', value: { ev: 412, p_success: 0.82, currency: 'USD' } },
			{ id: 2, type: 'RIGHTSIZING_CPU', pod_name: 'second', reason: 'r2', value: { ev: 35, p_success: 0.9, currency: 'USD' } }
		]);

		const { findAllByTestId, getByText } = render(DashboardPage);

		const values = await findAllByTestId('dashboard-expected-value');
		expect(values.map((node) => node.textContent.trim())).toEqual([
			'≈ $412/yr expected · 82% success',
			'≈ $35/yr expected · 90% success'
		]);
		expect(getByText('best').compareDocumentPosition(getByText('second')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
	});

	it('shows the allocated saving with the fixed-nodes note when nothing is billed back (IMP-03)', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			{
				id: 1,
				type: 'RIGHTSIZING_CPU',
				pod_name: 'api',
				reason: 'r1',
				potential_savings_cost: 0,
				projected_allocated_cost_annual: 236.52,
				projection_note: 'fixed nodes: frees capacity, bill unchanged until nodes are removed',
				value: { ev: 120, p_success: 0.8, currency: 'USD' }
			}
		]);
		const { findByTestId, getByText } = render(DashboardPage);
		expect((await findByTestId('dashboard-allocated-saving')).textContent).toMatch(/Allocated/);
		expect(getByText('fixed nodes: frees capacity, bill unchanged until nodes are removed')).toBeTruthy();
	});

	it('labels an unknown billable saving as allocated, never as projected (IMP-12 review)', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			{
				id: 1,
				type: 'RIGHTSIZING_CPU',
				pod_name: 'api',
				reason: 'r1',
				potential_savings_cost: 236.52,
				projected_billable_cost_annual: null,
				projected_allocated_cost_annual: 236.52,
				projection_note: 'billable saving unknown: cluster not observed yet',
				value: { ev: 120, p_success: 0.8, currency: 'USD' }
			}
		]);
		const { findByTestId, getByText, queryByText } = render(DashboardPage);
		expect((await findByTestId('dashboard-allocated-unknown')).textContent).toMatch(/^Allocated/);
		expect(getByText('billable saving unknown: cluster not observed yet')).toBeTruthy();
		expect(queryByText(/Projected \$237/)).toBeNull();
	});

	it('links each top recommendation to its detail page and shows its delivery state', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			{ id: 7, pod_name: 'api', owner_kind: 'Deployment', owner_name: 'api', namespace: 'shop', eligibility: { can_open_pr: true } },
			{
				id: 8,
				pod_name: 'worker',
				eligibility: { can_open_pr: false, blockers: [{ code: 'repository_not_allowlisted', message: 'Repository is not allowlisted.' }] }
			}
		]);

		const { findByRole, getAllByTestId } = render(DashboardPage);

		const link = await findByRole('link', { name: 'Deployment/api' });
		expect(link).toHaveAttribute('href', '/recommendations/7');
		expect(getAllByTestId('delivery-hint').map((node) => node.textContent.trim())).toEqual([
			'✓ Ready for a pull request',
			'• Repository is not allowlisted.'
		]);
	});

	it('shows realized ledger savings and projected opportunity side by side, never summed (K1)', async () => {
		api.getRecommendationSavings.mockResolvedValue({
			realized_to_date: { method: 'measured', basis: 'accrued', cost: 120, co2e_grams: 4000 },
			run_rate_annual: { method: 'measured', basis: 'annual_rate', cost: 0, co2e_grams: 0 },
			projected_annual: { method: 'projected', basis: 'annual_rate', cost: 0, co2e_grams: 0 },
			total_cost_saved: 999
		});
		api.getActiveRecommendations.mockResolvedValue([{ id: 1, pod_name: 'api', potential_savings_cost: 300 }]);

		const { findByTestId, container } = render(DashboardPage);

		expect(await findByTestId('realized-cost')).toHaveTextContent('$120');
		expect(await findByTestId('opportunity-cost')).toHaveTextContent('$300/yr');
		expect(container.textContent).not.toContain('$420');
		expect(container.textContent).not.toContain('$999');
		// Realized savings are the whole ledger, not the footprint period.
		expect(api.getRecommendationSavings.mock.calls.at(-1)[0]).not.toHaveProperty('last');
	});

	it('counts the changes in flight from open PRs and /api/v2/changes', async () => {
		api.getOpenPullRequests.mockResolvedValue([{ id: 1 }, { id: 2 }]);
		api.listChanges.mockResolvedValue({
			items: [{ state: 'applied' }, { state: 'measuring' }, { state: 'measuring' }],
			next_cursor: null
		});

		const { findByTestId } = render(DashboardPage);

		await waitFor(async () => expect(await findByTestId('inflight-prs')).toHaveTextContent('2'));
		expect(await findByTestId('inflight-applied')).toHaveTextContent('1');
		expect(await findByTestId('inflight-measuring')).toHaveTextContent('2');
		expect(api.listChanges.mock.calls.at(-1)[0].state).toEqual(['applied', 'measuring']);
	});

	it('explains the empty inbox and the evidence warm-up', async () => {
		const { findByTestId, unmount } = render(DashboardPage);
		expect(await findByTestId('inbox-empty')).toHaveTextContent('No open recommendation');
		unmount();

		const later = new Date(Date.now() + 30 * 3_600_000).toISOString();
		api.getActiveRecommendations.mockResolvedValue([
			{ id: 3, pod_name: 'api', eligibility: { can_open_pr: false, eligible_at: later, blockers: [] } }
		]);
		const second = render(DashboardPage);
		expect(await second.findByTestId('warm-up')).toHaveTextContent('1 recommendation becomes eligible for a pull request in ~30h');
	});
});
