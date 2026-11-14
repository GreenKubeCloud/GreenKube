/**
 * Change and ledger pages: the targets of the PR deep links (J1)
 * `/changes/{plan_id}` and `/ledger?change={change_id or plan_id}`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import ChangePage from '../../src/routes/changes/[plan_id]/+page.svelte';
import { load as loadChange } from '../../src/routes/changes/[plan_id]/+page.js';
import LedgerPage from '../../src/routes/ledger/+page.svelte';
import { load as loadLedger } from '../../src/routes/ledger/+page.js';
import * as api from '$lib/api.js';

vi.mock('$lib/api.js', () => {
	class ApiError extends Error {
		constructor(message, status) {
			super(message);
			this.name = 'ApiError';
			this.status = status;
		}
	}
	return {
		ApiError,
		getAutomationPlan: vi.fn(),
		getChange: vi.fn(),
		getChangeMeasurements: vi.fn(),
		getRecommendationPullRequests: vi.fn(),
		getRecommendationSavings: vi.fn(),
		listChanges: vi.fn()
	};
});

const now = Date.now();
const iso = (offsetHours) => new Date(now + offsetHours * 3_600_000).toISOString();
const notFound = () => new api.ApiError('Not found', 404);

const plan = {
	plan_id: 'plan-1',
	recommendation_ids: [12],
	status: 'synced',
	operation_status: 'succeeded',
	source: { controller: 'argocd/shop', repo: 'https://github.com/acme/deploy.git', base_branch: 'main', path: 'apps/shop', renderer: 'raw' },
	files: [{ path: 'apps/shop/deployment.yaml', operation: 'update' }],
	created_at: iso(-72)
};

const change = {
	change_id: 'chg-1',
	plan_id: 'plan-1',
	recommendation_ids: [12],
	pr_url: 'https://github.com/acme/deploy/pull/42',
	target: { namespace: 'shop', kind: 'Deployment', name: 'api' },
	rollout_completed_at: iso(-48),
	application_method: 'gitops_pr',
	state: 'realized',
	post_window: { start: iso(-47), end: iso(-1) },
	events: []
};

const measurement = {
	measurement_id: 'm-1',
	change_id: 'chg-1',
	strategy_key: 'request_rightsizing',
	strategy_version: '1.0',
	outcome: 'realized',
	decided_at: iso(-1),
	estimates: [
		{
			quantity: 'allocated_cost',
			unit: 'currency',
			claimed: true,
			annualized: 300,
			annualized_ci_low: 250,
			annualized_ci_high: 350,
			projected_annual: 400,
			outcome: 'realized'
		},
		{ quantity: 'co2e_g', unit: 'gCO2e', claimed: true, annualized: 9000, outcome: 'realized' },
		{ quantity: 'core_hours', unit: 'core-hours', claimed: false, annualized: 2190 }
	],
	reasons: ['Load-normalized against requests per second.'],
	recommendations: [{ recommendation_id: 12, outcome: 'realized' }]
};

const savings = {
	realized_to_date: { method: 'measured', basis: 'accrued', cost: 120, co2e_grams: 4000 },
	run_rate_annual: { method: 'measured', basis: 'annual_rate', cost: 300, co2e_grams: 9000 },
	projected_annual: { method: 'projected', basis: 'annual_rate', cost: 500, co2e_grams: 15000 },
	user_declared_annual: { method: 'user_declared', basis: 'annual_rate', cost: 0, co2e_grams: 0 }
};

beforeEach(() => {
	vi.clearAllMocks();
	api.getAutomationPlan.mockResolvedValue(plan);
	api.getChange.mockImplementation(async (id) => {
		if (id === 'chg-1') return change;
		throw notFound();
	});
	api.listChanges.mockResolvedValue({ items: [change], next_cursor: null });
	api.getChangeMeasurements.mockResolvedValue({ items: [measurement] });
	api.getRecommendationPullRequests.mockResolvedValue([
		{ id: 1, status: 'merged', sync_status: 'synced', pr_number: 42, pr_url: 'https://github.com/acme/deploy/pull/42', plan_id: 'plan-1' }
	]);
	api.getRecommendationSavings.mockResolvedValue(savings);
});

describe('/changes/[plan_id]', () => {
	it('loads the plan id from the route', () => {
		expect(loadChange({ params: { plan_id: 'plan-1' } })).toEqual({ planId: 'plan-1' });
	});

	it('shows the plan, its pull request, the applied change and its measurement', async () => {
		render(ChangePage, { props: { data: { planId: 'plan-1' } } });

		const changeSection = await screen.findByTestId('change');
		expect(changeSection).toHaveTextContent('Deployment shop/api');
		// "Realized" is reserved for billed savings (principle 3).
		expect(changeSection).toHaveTextContent('Saving measured');
		expect(changeSection).not.toHaveTextContent('Realized');
		expect(within(changeSection).getByTestId('measurement')).toHaveTextContent('Allocated cost');
		expect(screen.getByTestId('plan')).toHaveTextContent('https://github.com/acme/deploy.git');
		expect(screen.getByRole('link', { name: '#12' })).toHaveAttribute('href', '/recommendations/12');
		expect(screen.getByRole('link', { name: /Open the pull request #42/ })).toHaveAttribute(
			'href',
			'https://github.com/acme/deploy/pull/42'
		);
		expect(screen.getByRole('link', { name: /Ledger entries of this change/ })).toHaveAttribute('href', '/ledger?change=chg-1');
		expect(api.getChangeMeasurements).toHaveBeenCalledWith('chg-1', expect.anything());
	});

	it('explains a plan that is not applied yet', async () => {
		api.listChanges.mockResolvedValue({ items: [], next_cursor: null });
		render(ChangePage, { props: { data: { planId: 'plan-1' } } });
		expect(await screen.findByTestId('change-pending')).toHaveTextContent('Not applied yet');
	});

	it('keeps the applied change when the plan was removed by retention', async () => {
		api.getAutomationPlan.mockRejectedValue(notFound());
		render(ChangePage, { props: { data: { planId: 'plan-1' } } });
		expect(await screen.findByTestId('change')).toHaveTextContent('Deployment shop/api');
		expect(screen.getByTestId('plan')).toHaveTextContent('no longer stored');
	});

	it('shows not found when neither a plan nor a change exists', async () => {
		api.getAutomationPlan.mockRejectedValue(notFound());
		api.listChanges.mockResolvedValue({ items: [], next_cursor: null });
		render(ChangePage, { props: { data: { planId: 'plan-unknown' } } });
		expect(await screen.findByTestId('not-found')).toHaveTextContent('Change not found');
	});
});

describe('/ledger', () => {
	it('reads the ?change= filter', () => {
		expect(loadLedger({ url: new URL('http://x/ledger?change=plan-1') })).toEqual({ change: 'plan-1' });
		expect(loadLedger({ url: new URL('http://x/ledger') })).toEqual({ change: null });
	});

	it('shows each savings method apart, never summed', async () => {
		const { container } = render(LedgerPage, { props: { data: { change: null } } });

		expect(await screen.findByTestId('ledger-total-realized')).toHaveTextContent('$120');
		expect(screen.getByTestId('ledger-total-realized')).toHaveTextContent('Measured');
		expect(screen.getByTestId('ledger-total-pending')).toHaveTextContent('$500/yr');
		expect(screen.getByTestId('ledger-total-pending')).toHaveTextContent('Projected');
		expect(screen.getByText(/never added together/)).toBeInTheDocument();
		for (const total of ['$620', '$800', '$920']) expect(container.textContent).not.toContain(total);
	});

	it('names measured figures allocated, shows the realization factor and reserves "Realized" for billed savings', async () => {
		api.getRecommendationSavings.mockResolvedValue({ ...savings, realization: { realization_factor: 0.6 } });
		render(LedgerPage, { props: { data: { change: null } } });

		expect(await screen.findByTestId('ledger-total-realized')).toHaveTextContent('Measured allocated to date');
		expect(screen.getByTestId('ledger-total-run-rate')).toHaveTextContent('Measured allocated run rate');
		expect(screen.getByTestId('ledger-realization-factor')).toHaveTextContent('Realization factor 60%');
		expect(screen.queryByTestId('ledger-total-billed')).not.toBeInTheDocument();
		expect(screen.queryByText('Realized to date')).not.toBeInTheDocument();
	});

	it('shows realized billed savings apart when the server reports them', async () => {
		api.getRecommendationSavings.mockResolvedValue({
			...savings,
			realized_billed_to_date: { method: 'realized_billed', basis: 'accrued', cost: 40, co2e_grams: 1000 }
		});
		render(LedgerPage, { props: { data: { change: null } } });

		const billed = await screen.findByTestId('ledger-total-billed');
		expect(billed).toHaveTextContent('Realized (billed) savings');
		expect(billed).toHaveTextContent('$40');
	});

	it('lists applied changes and filters them by state', async () => {
		render(LedgerPage, { props: { data: { change: null } } });

		const list = await screen.findByTestId('changes-list');
		expect(within(list).getByRole('link', { name: 'Deployment shop/api' })).toHaveAttribute('href', '/ledger?change=chg-1');
		expect(within(list).getByRole('link', { name: 'plan' })).toHaveAttribute('href', '/changes/plan-1');

		await fireEvent.change(screen.getByLabelText('State'), { target: { value: 'measuring' } });
		await waitFor(() => expect(api.listChanges.mock.calls.at(-1)[0].state).toEqual(['measuring']));
	});

	it('pages through changes with the opaque cursor', async () => {
		api.listChanges
			.mockResolvedValueOnce({ items: [change], next_cursor: 'cursor-1' })
			.mockResolvedValueOnce({ items: [{ ...change, change_id: 'chg-2', plan_id: null }], next_cursor: null });
		render(LedgerPage, { props: { data: { change: null } } });

		await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
		await waitFor(() => expect(within(screen.getByTestId('changes-list')).getAllByRole('listitem')).toHaveLength(2));
		expect(api.listChanges.mock.calls.at(-1)[0].cursor).toBe('cursor-1');
	});

	it('explains an empty ledger', async () => {
		api.listChanges.mockResolvedValue({ items: [], next_cursor: null });
		render(LedgerPage, { props: { data: { change: null } } });
		expect(await screen.findByTestId('changes-empty')).toHaveTextContent('No applied change yet');
	});

	it('opens one change from a plan id deep link with its measurement and split', async () => {
		render(LedgerPage, { props: { data: { change: 'plan-1' } } });

		const selected = await screen.findByTestId('selected-change');
		await waitFor(() => expect(within(selected).getByTestId('measurement')).toBeInTheDocument());
		expect(selected).toHaveTextContent('Deployment shop/api');
		expect(selected).toHaveTextContent('Measured /yr');
		expect(selected).toHaveTextContent('Load-normalized against requests per second.');
		expect(within(selected).getByRole('link', { name: '#12' })).toHaveAttribute('href', '/recommendations/12');
		expect(within(selected).getByRole('link', { name: /Plan and pull request/ })).toHaveAttribute('href', '/changes/plan-1');
		// The plan's change is filtered on the server (GET /changes?plan_id=), not scanned.
		expect(api.listChanges).toHaveBeenCalledWith(expect.objectContaining({ planId: 'plan-1', limit: 1 }));
	});

	it('explains a change that is not recorded yet', async () => {
		api.listChanges.mockResolvedValue({ items: [], next_cursor: null });
		render(LedgerPage, { props: { data: { change: 'plan-9' } } });
		expect(await screen.findByTestId('selected-missing')).toHaveTextContent('No applied change matches this id yet');
	});
});
