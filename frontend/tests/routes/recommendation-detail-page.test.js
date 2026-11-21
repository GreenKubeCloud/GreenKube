/**
 * Recommendation detail page (K3): the target of PR deep links
 * `/recommendations/{id}` (J1).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/svelte';
import DetailPage from '../../src/routes/recommendations/[id]/+page.svelte';
import { load } from '../../src/routes/recommendations/[id]/+page.js';
import * as api from '#lib/api.js';
// GET /api/v2/recommendations/12 generated from the FastAPI app (tests/api/test_ui_contract_fixtures.py).
import V2_DETAIL from '../fixtures/api/recommendation_v2_detail.json';

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
		getRecommendation: vi.fn(),
		getRecommendationEvents: vi.fn(),
		getRecommendationPullRequests: vi.fn(),
		getRecommendationV2: vi.fn(),
		getManualGuide: vi.fn(),
		markAppliedManually: vi.fn()
	};
});

const now = Date.now();
const iso = (offsetHours) => new Date(now + offsetHours * 3_600_000).toISOString();

const record = {
	id: 12,
	type: 'RIGHTSIZING_CPU',
	pod_name: 'api-7d9',
	owner_kind: 'Deployment',
	owner_name: 'api',
	namespace: 'shop',
	container_name: 'app',
	source: 'greenkube',
	status: 'active',
	risk_level: 'low',
	reason: 'CPU p95 is 20% of the request.',
	current_cpu_request_millicores: 500,
	recommended_cpu_request_millicores: 250,
	potential_savings_cost: 120,
	value: { ev: 90, p_success: 0.8, value_annual: 110, incident_cost: 5, effort_cost: 10, currency: 'USD' },
	impact: {
		model_version: '1.0',
		currency: 'USD',
		allocated_cost_annual: 120,
		expected_billable_cost_annual: 60,
		realization_factor: 0.5,
		co2e_expected_annual_g: 4500,
		co2e_modeled: true,
		cost_breakdown_annual: { cpu: 120 },
		co2e_components_annual_g: { operational_energy: 6000, idle_share: 1000, embodied: 2000 }
	},
	evidence: {
		observation_window_start: iso(-14 * 24),
		observation_window_end: iso(0),
		sample_count: 2000,
		coverage_ratio: 0.97,
		cpu_usage: { avg: 80, p50: 70, p95: 100, p99: 140, max: 200, sample_count: 2000 },
		restart_count: 0,
		oom_events: 0,
		rollback_conditions: [{ metric: 'cpu_throttling', comparator: 'gt', threshold: 0.2, description: 'Throttling above 20%' }],
		proposed_diff: '- cpu: 500m\n+ cpu: 250m'
	},
	eligibility: {
		can_open_pr: false,
		blockers: [
			{
				code: 'repository_not_allowlisted',
				message: 'The repository is not allowlisted.',
				fix: 'Allow it on the setup page.'
			}
		],
		warnings: [],
		eligible_at: iso(30)
	}
};

describe('recommendation detail page', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.getRecommendation.mockResolvedValue(record);
		// A server without /api/v2 (or a v1-only record): the page keeps the v1 fields.
		api.getRecommendationV2.mockRejectedValue(new api.ApiError('Not Found', 404));
		api.getRecommendationEvents.mockResolvedValue([
			{ event_type: 'created', actor: 'system', created_at: iso(-48) },
			{ event_type: 'refreshed', actor: 'system', created_at: iso(-24) },
			{ event_type: 'refreshed', actor: 'system', created_at: iso(-1) }
		]);
		api.getRecommendationPullRequests.mockResolvedValue([
			{ id: 1, status: 'open', repo: 'acme/deploy', pr_number: 42, pr_url: 'https://github.com/acme/deploy/pull/42', plan_id: 'plan-1' }
		]);
	});

	it('shows the unknown OOM history warning with its cause on an eligible recommendation', async () => {
		api.getRecommendation.mockResolvedValue({
			...record,
			eligibility: {
				can_open_pr: true,
				delivery_modes: ['direct', 'manual'],
				blockers: [],
				warnings: [
					{
						code: 'oom_history_unknown',
						message: 'No usage summary covers this container, so its OOM-kill and restart history could not be read.',
						fix: 'Before merging, check the container: no recent restarts, no OOMKilled last state.'
					}
				]
			}
		});
		render(DetailPage, { props: { data: { id: '12' } } });

		const warning = await screen.findByTestId('oom-history-warning');
		expect(warning).toHaveTextContent('OOM history unknown');
		expect(warning).toHaveTextContent('No usage summary covers this container');
		expect(warning).toHaveTextContent('Before merging');
	});

	it('loads the id from the route', () => {
		expect(load({ params: { id: '12' } })).toEqual({ id: '12' });
	});

	it('shows the proposal, projected impact, evidence, blockers with fixes and the timeline', async () => {
		render(DetailPage, { props: { data: { id: '12' } } });

		expect(await screen.findByRole('heading', { level: 1, name: 'Deployment/api' })).toBeInTheDocument();
		expect(api.getRecommendation).toHaveBeenCalledWith('12', expect.objectContaining({ signal: expect.any(AbortSignal) }));
		expect(screen.getByText('Open')).toBeInTheDocument();

		const impact = screen.getByTestId('impact');
		expect(within(impact).getByText('Projected')).toBeInTheDocument();
		expect(within(impact).getByText('Operational energy')).toBeInTheDocument();
		expect(within(impact).getByText('Embodied (hardware)')).toBeInTheDocument();
		expect(within(impact).getByText('50%')).toBeInTheDocument();

		const eligibility = screen.getByTestId('eligibility');
		expect(eligibility).toHaveTextContent('Cannot become a pull request yet');
		expect(eligibility).toHaveTextContent('repository_not_allowlisted');
		expect(eligibility).toHaveTextContent('Fix: Allow it on the setup page.');
		expect(eligibility).toHaveTextContent('eligible in ~30h');

		const evidence = screen.getByTestId('evidence');
		expect(evidence).toHaveTextContent('2000');
		expect(evidence).toHaveTextContent('97%');
		expect(evidence).toHaveTextContent('Throttling above 20%');

		const timeline = screen.getByTestId('timeline');
		expect(within(timeline).getAllByRole('listitem')).toHaveLength(2);
		expect(timeline).toHaveTextContent('×2');
	});

	it('offers the manual steps of an active recommendation without loading them (H9)', async () => {
		render(DetailPage, { props: { data: { id: '12' } } });

		const manual = await screen.findByTestId('manual-apply');
		expect(within(manual).getByRole('button', { name: 'Show manual steps' })).toBeInTheDocument();
		expect(api.getManualGuide).not.toHaveBeenCalled();
	});

	it('links the pull request, its change and the ledger (J1 deep links)', async () => {
		render(DetailPage, { props: { data: { id: '12' } } });

		const prLink = await screen.findByRole('link', { name: 'acme/deploy #42' });
		expect(prLink).toHaveAttribute('href', 'https://github.com/acme/deploy/pull/42');
		expect(prLink).toHaveAttribute('rel', 'noopener noreferrer');
		expect(screen.getByRole('link', { name: 'Change →' })).toHaveAttribute('href', '/changes/plan-1');
		expect(screen.getByRole('link', { name: /Ledger entries of plan plan-1/ })).toHaveAttribute('href', '/ledger?change=plan-1');
	});

	it('never links a pull request URL that is not http(s)', async () => {
		api.getRecommendationPullRequests.mockResolvedValue([
			{ id: 1, status: 'open', repo: 'acme/deploy', pr_url: 'javascript:alert(1)' }
		]);
		render(DetailPage, { props: { data: { id: '12' } } });
		await screen.findByTestId('pull-requests');
		expect(screen.queryByRole('link', { name: /acme\/deploy/ })).not.toBeInTheDocument();
		expect(screen.getByText('acme/deploy')).toBeInTheDocument();
	});

	it('hides the consensus section for a v1 record and shows it for v2', async () => {
		const { unmount } = render(DetailPage, { props: { data: { id: '12' } } });
		await screen.findByTestId('evidence');
		expect(screen.queryByTestId('consensus')).not.toBeInTheDocument();
		unmount();

		api.getRecommendation.mockResolvedValue({
			...record,
			revision: 2,
			consensus: {
				policy: 'safest',
				resource: 'cpu_request_m',
				value: 300,
				chosen_from: ['vpa'],
				agreement: 0.4,
				contested: true,
				reasons: ['agreement below 0.5'],
				spread: { cpu_request_m: { vpa: 300, krr: 120 } },
				votes: [
					{ source_id: 'vpa', family: 'vpa', value: 300, counted: true, supports_chosen: true },
					{ source_id: 'goldilocks', family: 'vpa', value: 300, counted: false }
				]
			}
		});
		render(DetailPage, { props: { data: { id: '12' } } });
		const consensus = await screen.findByTestId('consensus');
		expect(consensus).toHaveTextContent('Contested');
		expect(consensus).toHaveTextContent('chose 300m');
		expect(consensus).toHaveTextContent('vpa 300m');
		expect(consensus).toHaveTextContent('krr 120m');
		expect(consensus).toHaveTextContent('agreement below 0.5');
		expect(within(screen.getByTestId('consensus-votes')).getByText(/not counted/)).toBeInTheDocument();
		expect(screen.getByText('Revision 2')).toBeInTheDocument();
	});

	it('shows the v2 consensus, provenance, revision and queued newer advice from /api/v2 (E6, E7)', async () => {
		api.getRecommendationV2.mockResolvedValue(V2_DETAIL);
		render(DetailPage, { props: { data: { id: '12' } } });

		const consensus = await screen.findByTestId('consensus');
		expect(api.getRecommendationV2).toHaveBeenCalledWith('12', expect.objectContaining({ signal: expect.any(AbortSignal) }));
		// Contested: the reasons the server gave.
		expect(consensus).toHaveTextContent('Contested');
		expect(consensus).toHaveTextContent('agreement 0.40 is below 0.5');
		expect(consensus).toHaveTextContent('sources disagree on the direction');
		expect(consensus).toHaveTextContent('chose 300m');
		expect(consensus).toHaveTextContent('3 independent votes');
		// Every vote, with why a Goldilocks/VPA duplicate is not counted.
		const votes = within(screen.getByTestId('consensus-votes'));
		expect(votes.getAllByRole('row')).toHaveLength(5);
		expect(votes.getByText(/Same engine as another source, not counted/)).toBeInTheDocument();
		expect(votes.getByText('push:ci')).toBeInTheDocument();
		// Provenance badges in the server's order (chosen source first).
		const badges = within(screen.getByTestId('source-badges')).getAllByRole('listitem').map((item) => item.textContent);
		expect(badges).toEqual(['goldilocks', 'vpa', 'krr', 'push:ci']);
		// Revision 3 is current; revision 4 waits behind the open plan.
		expect(screen.getByText('Revision 3')).toBeInTheDocument();
		expect(screen.getByText('Newer advice: revision 4')).toBeInTheDocument();
		expect(screen.getByTestId('queued-revision')).toHaveTextContent('CPU request 260m');
		expect(screen.getByTestId('revisions')).toHaveTextContent('Revision 2 · superseded');
	});

	it('shows a not-found state for an unknown or malformed id', async () => {
		api.getRecommendation.mockRejectedValue(new api.ApiError('Recommendation 99 not found.', 404));
		const { unmount } = render(DetailPage, { props: { data: { id: '99' } } });
		expect(await screen.findByTestId('not-found')).toHaveTextContent('Recommendation not found');
		unmount();

		vi.clearAllMocks();
		render(DetailPage, { props: { data: { id: 'abc' } } });
		expect(await screen.findByTestId('not-found')).toBeInTheDocument();
		expect(api.getRecommendation).not.toHaveBeenCalled();
	});

	it('reports a server error without hiding it as not found', async () => {
		api.getRecommendation.mockRejectedValue(new api.ApiError('Database unavailable', 503));
		render(DetailPage, { props: { data: { id: '12' } } });
		expect(await screen.findByRole('alert')).toHaveTextContent('Database unavailable');
	});

	it('shows measured savings apart from the projection', async () => {
		api.getRecommendation.mockResolvedValue({
			...record,
			status: 'verified',
			measured_cost_saved: 70,
			savings_realized: true,
			verification_status: 'verified'
		});
		render(DetailPage, { props: { data: { id: '12' } } });
		const measurement = await screen.findByTestId('measurement');
		expect(measurement).toHaveTextContent('Measured');
		expect(measurement).toHaveTextContent('$70/yr');
		expect(measurement).not.toHaveTextContent('$120');
	});
});
