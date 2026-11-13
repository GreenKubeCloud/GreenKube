/**
 * Recommendations page daily use: read-only page loads and scan runs (K9,
 * BUG-25), server filters and truthful ordering (K2, BUG-20), ranking on
 * cards, and preview discard on close (D6, BUG-48).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import RecommendationsPage from '../../src/routes/recommendations/+page.svelte';
import * as api from '$lib/api.js';
import { selectedNamespace } from '$lib/stores.js';
import { resetOptimizationRunsSupport } from '$lib/optimizationRuns.js';

vi.mock('$lib/api.js', () => ({
	approveAutomationPlan: vi.fn(),
	cancelAutomationPlan: vi.fn(),
	createAutomationPlan: vi.fn(),
	discardAutomationPlan: vi.fn(),
	getActiveRecommendations: vi.fn(),
	getAppliedRecommendations: vi.fn(),
	getAutomationOperation: vi.fn(),
	getAutomationPlan: vi.fn(),
	getAutomationStatus: vi.fn(),
	getIgnoredRecommendations: vi.fn(),
	getNamespaces: vi.fn(),
	getOpenPullRequests: vi.fn(),
	getOptimizationRun: vi.fn(),
	getRecommendationEvents: vi.fn(),
	getRecommendationSavings: vi.fn(),
	getTopRecommendations: vi.fn(),
	ignoreRecommendation: vi.fn(),
	listOptimizationRuns: vi.fn(),
	previewAutomationPlanRollback: vi.fn(),
	reportPlanDelivery: vi.fn(),
	planPatchUrl: vi.fn((id) => `/api/v1/automation/plans/${id}/patch`),
	requestOptimizationRun: vi.fn(),
	unignoreRecommendation: vi.fn()
}));

function rec(id, overrides = {}) {
	return {
		id,
		type: 'RIGHTSIZING_CPU',
		scope: 'workload',
		namespace: 'payments',
		pod_name: `pod-${id}`,
		source: 'greenkube',
		capability: 'cpu_rightsizing',
		risk_level: 'low',
		reason: `Reason ${id}`,
		...overrides
	};
}

function actionReady(id) {
	return rec(id, {
		owner_kind: 'Deployment',
		owner_name: 'api',
		container_name: 'api',
		eligibility: { can_open_pr: true, delivery_modes: ['direct', 'manual'], blockers: [], warnings: [], recommendation_ids: [id] },
		action: {
			version: 3,
			action_id: `action-${id}`,
			recommendation_ids: [id],
			target: { api_version: 'apps/v1', kind: 'Deployment', namespace: 'payments', name: 'api' },
			operations: [{ op: 'set_container_resources', container: 'api', resources: { requests: { cpu: '250m' } } }],
			constraints: { container_targeted: true }
		}
	});
}

const validatedPlan = {
	plan_id: 'pln-1',
	status: 'validated',
	digest: 'sha256:digest',
	expires_at: '2099-01-01T00:00:00Z',
	source: { controller: 'argocd/payments', repo: 'https://git.example/gitops.git', path: 'apps/payments', renderer: 'raw' },
	files: [{ path: 'apps/payments/deployment.yaml', operation: 'update' }],
	diff: 'diff --git a/apps/payments/deployment.yaml',
	rendered_diff: 'Deployment/payments/api changed',
	validation: { passed: true }
};

function cardTitles() {
	return screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent.trim());
}

async function flush() {
	await act(async () => {
		await vi.advanceTimersByTimeAsync(0);
	});
}

beforeEach(() => {
	vi.clearAllMocks();
	resetOptimizationRunsSupport();
	selectedNamespace.set('');
	api.getActiveRecommendations.mockResolvedValue([rec(1)]);
	api.getIgnoredRecommendations.mockResolvedValue([]);
	api.getAppliedRecommendations.mockResolvedValue([]);
	api.getRecommendationSavings.mockResolvedValue({});
	api.getOpenPullRequests.mockResolvedValue([]);
	api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true });
	api.getNamespaces.mockResolvedValue(['payments', 'search']);
	api.listOptimizationRuns.mockResolvedValue([]);
	api.discardAutomationPlan.mockResolvedValue({ discarded: true, status: 204 });
	api.getTopRecommendations.mockResolvedValue([]);
});

afterEach(() => {
	vi.useRealTimers();
	selectedNamespace.set('');
});

describe('page load (K9, BUG-25)', () => {
	it('reads recommendations without scheduling a scan', async () => {
		render(RecommendationsPage);
		await screen.findByText('Reason 1');

		const [params] = api.getActiveRecommendations.mock.calls[0];
		expect(params).not.toHaveProperty('refresh');
		expect(params.signal).toBeInstanceOf(AbortSignal);
		expect(api.requestOptimizationRun).not.toHaveBeenCalled();
	});

	it('links each active card to its manual steps (H9)', async () => {
		render(RecommendationsPage);
		await screen.findByText('Reason 1');

		const link = screen.getByTestId('apply-manually');
		expect(link).toHaveAttribute('href', '/recommendations/1#manual');
		expect(link).toHaveTextContent('Apply manually');
	});

	it('aborts in-flight reads when the page is left', async () => {
		api.getActiveRecommendations.mockReturnValue(new Promise(() => {}));
		const { unmount } = render(RecommendationsPage);
		await vi.waitFor(() => expect(api.getActiveRecommendations).toHaveBeenCalled());
		const signals = [
			api.getActiveRecommendations.mock.calls[0][0].signal,
			api.getIgnoredRecommendations.mock.calls[0][0].signal,
			api.getAppliedRecommendations.mock.calls[0][0].signal,
			api.getRecommendationSavings.mock.calls[0][0].signal,
			api.getOpenPullRequests.mock.calls[0][0].signal
		];

		unmount();

		expect(signals.every((signal) => signal.aborted)).toBe(true);
	});

	it('scopes every namespaced list to the selected namespace', async () => {
		selectedNamespace.set('payments');
		render(RecommendationsPage);
		await screen.findByText('Reason 1');

		for (const helper of [
			api.getActiveRecommendations,
			api.getIgnoredRecommendations,
			api.getAppliedRecommendations,
			api.getRecommendationSavings
		]) {
			expect(helper.mock.calls.at(-1)[0].namespace).toBe('payments');
		}
	});

	it('reloads the results when a scan that was already running completes', async () => {
		vi.useFakeTimers();
		api.listOptimizationRuns.mockResolvedValue([{ run_id: 5, status: 'running' }]);
		api.getOptimizationRun.mockResolvedValue({ run_id: 5, status: 'succeeded', recommendation_count: 2 });

		render(RecommendationsPage);
		await flush();
		expect(screen.getByText(/Recommendation scan #5 is running/)).toBeInTheDocument();
		expect(api.getActiveRecommendations).toHaveBeenCalledTimes(1);

		api.getActiveRecommendations.mockResolvedValue([rec(1), rec(2)]);
		await act(async () => {
			await vi.advanceTimersByTimeAsync(2000);
		});

		expect(api.getOptimizationRun).toHaveBeenCalledWith(5, expect.objectContaining({ signal: expect.any(AbortSignal) }));
		expect(api.getActiveRecommendations).toHaveBeenCalledTimes(2);
		expect(screen.getByText('Reason 2')).toBeInTheDocument();
		expect(screen.getByText(/Recommendation scan #5 completed with 2 recommendations/)).toBeInTheDocument();
	});
});

describe('rescan (K9, E1)', () => {
	it('requests a run, polls it and reloads on completion', async () => {
		vi.useFakeTimers();
		api.requestOptimizationRun.mockResolvedValue({ run_id: 12, status: 'queued' });
		api.getOptimizationRun
			.mockResolvedValueOnce({ run_id: 12, status: 'running' })
			.mockResolvedValueOnce({ run_id: 12, status: 'succeeded', recommendation_count: 1 });
		render(RecommendationsPage);
		await flush();

		await fireEvent.click(screen.getByRole('button', { name: /Rescan/ }));
		await flush();

		expect(api.requestOptimizationRun).toHaveBeenCalledTimes(1);
		expect(screen.getByText(/Recommendation scan #12 is queued/)).toBeInTheDocument();
		expect(screen.getByRole('button', { name: /Scanning/ })).toBeDisabled();

		await act(async () => {
			await vi.advanceTimersByTimeAsync(2000);
		});
		expect(screen.getByText(/Recommendation scan #12 is running/)).toBeInTheDocument();
		expect(api.getActiveRecommendations).toHaveBeenCalledTimes(1);

		await act(async () => {
			await vi.advanceTimersByTimeAsync(3000);
		});
		expect(api.getActiveRecommendations).toHaveBeenCalledTimes(2);
		expect(screen.getByText(/Recommendation scan #12 completed with 1 recommendation\./)).toBeInTheDocument();
		expect(screen.getByRole('button', { name: /Rescan/ })).toBeEnabled();
	});

	it('reports a failed run', async () => {
		vi.useFakeTimers();
		api.requestOptimizationRun.mockResolvedValue({ run_id: 13, status: 'queued' });
		api.getOptimizationRun.mockResolvedValue({ run_id: 13, status: 'failed', error: 'Prometheus unreachable' });
		render(RecommendationsPage);
		await flush();

		await fireEvent.click(screen.getByRole('button', { name: /Rescan/ }));
		await act(async () => {
			await vi.advanceTimersByTimeAsync(2000);
		});

		expect(screen.getByRole('alert')).toHaveTextContent('Recommendation scan #13 failed: Prometheus unreachable');
	});

	it.each([404, 405])(
		'still requests a scan when the optional run list endpoint is missing (%s)',
		async (status) => {
			api.listOptimizationRuns.mockRejectedValue(Object.assign(new Error('Missing'), { status }));
			api.requestOptimizationRun.mockResolvedValue({ run_id: 21, status: 'queued' });
			api.getOptimizationRun.mockReturnValue(new Promise(() => {}));
			render(RecommendationsPage);
			await screen.findByText('Reason 1');
			await vi.waitFor(() => expect(api.listOptimizationRuns).toHaveBeenCalled());

			await fireEvent.click(screen.getByRole('button', { name: /Rescan/ }));

			await vi.waitFor(() => expect(api.requestOptimizationRun).toHaveBeenCalledTimes(1));
			expect(await screen.findByText(/Recommendation scan #21 is queued/)).toBeInTheDocument();
		}
	);

	it('falls back to a plain reload on a server without the runs API', async () => {
		api.requestOptimizationRun.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }));
		render(RecommendationsPage);
		await screen.findByText('Reason 1');

		await fireEvent.click(screen.getByRole('button', { name: /Rescan/ }));

		await vi.waitFor(() => expect(api.getActiveRecommendations).toHaveBeenCalledTimes(2));
		expect(api.getOptimizationRun).not.toHaveBeenCalled();
		expect(api.getActiveRecommendations.mock.calls[1][0]).not.toHaveProperty('refresh');
		expect(screen.queryByText(/Recommendation scan #/)).not.toBeInTheDocument();
	});
});

describe('filters (K2)', () => {
	it.each([
		['Filter by source', 'vpa', 'source'],
		['Filter by risk level', 'high', 'risk_level'],
		['Filter by capability', 'memory_rightsizing', 'capability']
	])('%s is sent to the API', async (label, value, param) => {
		render(RecommendationsPage);
		await screen.findByText('Reason 1');

		await fireEvent.change(screen.getByLabelText(label), { target: { value } });

		await vi.waitFor(() =>
			expect(api.getActiveRecommendations.mock.calls.at(-1)[0]).toMatchObject({ [param]: value })
		);
	});

	it('filters by namespace through the shared namespace selection', async () => {
		render(RecommendationsPage);
		await screen.findByText('Reason 1');
		const select = screen.getByLabelText('Filter by namespace');
		await vi.waitFor(() => expect(within(select).getByRole('option', { name: 'search' })).toBeInTheDocument());

		await fireEvent.change(select, { target: { value: 'search' } });

		expect(get(selectedNamespace)).toBe('search');
		await vi.waitFor(() => expect(api.getActiveRecommendations.mock.calls.at(-1)[0].namespace).toBe('search'));
		expect(api.getIgnoredRecommendations.mock.calls.at(-1)[0].namespace).toBe('search');
	});

	it('applies the same filters to the ignored list', async () => {
		api.getIgnoredRecommendations.mockResolvedValue([
			rec(21, { source: 'vpa', reason: 'Ignored VPA' }),
			rec(22, { source: 'greenkube', reason: 'Ignored GreenKube' })
		]);
		render(RecommendationsPage);
		await screen.findByText('Reason 1');

		await fireEvent.change(screen.getByLabelText('Filter by source'), { target: { value: 'vpa' } });
		await fireEvent.click(screen.getByRole('button', { name: /Ignored/ }));

		expect(await screen.findByText('Ignored VPA')).toBeInTheDocument();
		expect(screen.queryByText('Ignored GreenKube')).not.toBeInTheDocument();
	});

	it('clears every filter at once', async () => {
		render(RecommendationsPage);
		await screen.findByText('Reason 1');
		await fireEvent.change(screen.getByLabelText('Filter by risk level'), { target: { value: 'high' } });

		await fireEvent.click(await screen.findByRole('button', { name: /Clear filters/ }));

		await vi.waitFor(() =>
			expect(api.getActiveRecommendations.mock.calls.at(-1)[0]).toMatchObject({ risk_level: undefined })
		);
		expect(screen.getByLabelText('Filter by risk level')).toHaveValue('');
	});
});

describe('ordering and ranking (K2, BUG-20)', () => {
	const serverOrder = [
		rec(1, {
			pod_name: 'first-by-score',
			potential_savings_cost: 5,
			potential_savings_co2e_grams: 900,
			ranking_score: 412.4,
			ranking_factors: { carbon: 400, cost: 20, incident_risk: -7.6 },
			value: { ev: 412.4, p_success: 0.82, currency: 'EUR', lane: 'savings' }
		}),
		rec(2, {
			pod_name: 'second-by-score',
			potential_savings_cost: 50,
			potential_savings_co2e_grams: 10,
			ranking_score: 35,
			ranking_factors: { cost: 41, incident_risk: -6 },
			value: { ev: 35, p_success: 0.9, currency: 'EUR', lane: 'savings' }
		})
	];

	it('labels the default order truthfully and keeps the server order', async () => {
		api.getActiveRecommendations.mockResolvedValue(serverOrder);
		render(RecommendationsPage);
		await screen.findByText('first-by-score');

		const sort = screen.getByLabelText('Sort recommendations');
		expect(sort).toHaveValue('server');
		expect(within(sort).getByRole('option', { name: 'Expected value (default)' })).toBeInTheDocument();
		expect(within(sort).queryByRole('option', { name: 'Projected savings' })).not.toBeInTheDocument();
		expect(cardTitles()).toEqual(['first-by-score', 'second-by-score']);
	});

	it('sorts by projected cost savings when asked', async () => {
		api.getActiveRecommendations.mockResolvedValue(serverOrder);
		render(RecommendationsPage);
		await screen.findByText('first-by-score');

		await fireEvent.change(screen.getByLabelText('Sort recommendations'), { target: { value: 'cost' } });

		expect(cardTitles()).toEqual(['second-by-score', 'first-by-score']);
		expect(api.getTopRecommendations).not.toHaveBeenCalled();
	});

	it('shows the expected value, success probability and top factor on each card (I1)', async () => {
		api.getActiveRecommendations.mockResolvedValue(serverOrder);
		render(RecommendationsPage);
		await screen.findByText('first-by-score');

		const [first, second] = screen.getAllByTestId('ranking-score');
		expect(first).toHaveTextContent('≈ €412/yr expected · 82% success · top factor: Carbon value');
		expect(first).toHaveAttribute('title', 'Carbon value €400 · Cost savings €20 · Incident risk -€7.60');
		expect(second).toHaveTextContent('≈ €35/yr expected · 90% success · top factor: Cost savings');
	});

	it('falls back to the plain score from an older server', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			rec(1, { pod_name: 'legacy', ranking_score: 0.9, ranking_factors: { carbon: 0.5, cost: 0.1 } })
		]);
		render(RecommendationsPage);
		await screen.findByText('legacy');

		expect(screen.getByTestId('ranking-score')).toHaveTextContent('Score 0.90 · top factor: Carbon value');
	});

	it('marks reliability recommendations', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			rec(1, { pod_name: 'oom-prone', value: { ev: 12, p_success: 0.9, currency: 'USD', lane: 'reliability' } })
		]);
		render(RecommendationsPage);
		await screen.findByText('oom-prone');

		expect(screen.getByText('Reliability')).toBeInTheDocument();
	});

	it('re-ranks with a profile through the server', async () => {
		api.getActiveRecommendations.mockImplementation(async ({ profile } = {}) =>
			profile === 'cost_first' ? [...serverOrder].reverse() : serverOrder
		);
		render(RecommendationsPage);
		await screen.findByText('first-by-score');

		await fireEvent.change(screen.getByLabelText('Sort recommendations'), {
			target: { value: 'profile:cost_first' }
		});

		await vi.waitFor(() => expect(cardTitles()).toEqual(['second-by-score', 'first-by-score']));
		expect(api.getActiveRecommendations).toHaveBeenLastCalledWith(
			expect.objectContaining({ profile: 'cost_first', signal: expect.any(AbortSignal) })
		);
		expect(api.getTopRecommendations).not.toHaveBeenCalled();
	});
});

describe('plan preview lifecycle (D6, BUG-48)', () => {
	beforeEach(() => {
		api.getActiveRecommendations.mockResolvedValue([actionReady(7)]);
		api.createAutomationPlan.mockResolvedValue(validatedPlan);
	});

	it('discards an unapproved preview when the dialog closes', async () => {
		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await screen.findByText('sha256:digest');

		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));

		await vi.waitFor(() => expect(api.discardAutomationPlan).toHaveBeenCalledWith('pln-1'));
		expect(screen.queryByText('Review automation plan')).not.toBeInTheDocument();
	});

	it('discards the preview when the dialog is dismissed with Escape', async () => {
		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await screen.findByText('sha256:digest');

		await fireEvent.keyDown(window, { key: 'Escape' });

		await vi.waitFor(() => expect(api.discardAutomationPlan).toHaveBeenCalledWith('pln-1'));
	});

	it('discards a preview that arrives after the dialog was closed', async () => {
		let resolvePlan;
		api.createAutomationPlan.mockReturnValue(
			new Promise((resolve) => {
				resolvePlan = resolve;
			})
		);
		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await screen.findByText(/Preparing preview/);

		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
		expect(api.discardAutomationPlan).not.toHaveBeenCalled();
		resolvePlan(validatedPlan);

		await vi.waitFor(() => expect(api.discardAutomationPlan).toHaveBeenCalledWith('pln-1'));
		expect(screen.queryByText('Review automation plan')).not.toBeInTheDocument();
	});

	it('keeps an approved plan when the dialog closes', async () => {
		api.approveAutomationPlan.mockResolvedValue({ ...validatedPlan, status: 'approved' });
		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve and open PR' }));
		await screen.findByText(/Plan approved/);

		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));

		expect(api.discardAutomationPlan).not.toHaveBeenCalled();
	});

	it.each([
		['the Close button', () => fireEvent.click(screen.getByRole('button', { name: 'Close' }))],
		['Escape', () => fireEvent.keyDown(window, { key: 'Escape' })]
	])('never discards a plan whose approval is in flight when closed with %s', async (_label, close) => {
		let resolveApproval;
		api.approveAutomationPlan.mockReturnValue(
			new Promise((resolve) => {
				resolveApproval = resolve;
			})
		);
		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve and open PR' }));
		await vi.waitFor(() => expect(api.approveAutomationPlan).toHaveBeenCalledWith('pln-1', 'sha256:digest'));

		await close();
		resolveApproval({ ...validatedPlan, status: 'approved' });
		await vi.waitFor(() => expect(screen.queryByText('Review automation plan')).not.toBeInTheDocument());
		await act(async () => {
			await Promise.resolve();
		});

		expect(api.discardAutomationPlan).not.toHaveBeenCalled();
	});

	it('never discards a plan whose approval is in flight when the page is left', async () => {
		api.approveAutomationPlan.mockReturnValue(new Promise(() => {}));
		const { unmount } = render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve and open PR' }));
		await vi.waitFor(() => expect(api.approveAutomationPlan).toHaveBeenCalled());

		unmount();
		await act(async () => {
			await Promise.resolve();
		});

		expect(api.discardAutomationPlan).not.toHaveBeenCalled();
	});

	it('never discards a plan once its approval was requested, even if the approval failed', async () => {
		api.approveAutomationPlan.mockRejectedValue(
			Object.assign(new Error('Approval could not be confirmed.'), { status: 503 })
		);
		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve and open PR' }));
		await screen.findByText(/Approval could not be confirmed/);

		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
		await act(async () => {
			await Promise.resolve();
		});

		expect(api.discardAutomationPlan).not.toHaveBeenCalled();
	});

	it('closes the dialog even when the preview cannot be discarded', async () => {
		api.discardAutomationPlan.mockRejectedValue(Object.assign(new Error('Conflict'), { status: 409 }));
		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await screen.findByText('sha256:digest');

		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));

		expect(screen.queryByText('Review automation plan')).not.toBeInTheDocument();
		expect(screen.queryByRole('alert')).not.toBeInTheDocument();
	});

	it('explains WORKLOAD_PLAN_ALREADY_EXISTS from an older server', async () => {
		api.createAutomationPlan.mockRejectedValue(
			Object.assign(new Error('Another plan for this workload is still active.'), {
				status: 409,
				code: 'workload_plan_already_exists',
				policyResult: {
					allowed: false,
					checks: [{ gate: 'active_plan', state: 'rejected', message: 'Another plan for this workload is still active.' }],
					rejections: [{ code: 'workload_plan_already_exists', detail: 'Resolve or cancel the existing workload plan.' }]
				}
			})
		);
		render(RecommendationsPage);

		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		const alert = await screen.findByRole('alert');
		expect(alert).toHaveTextContent('Another plan for this workload is still active');
		expect(alert).toHaveTextContent('Unapproved previews expire after 30 minutes');
		expect(screen.getByRole('button', { name: 'Re-plan' })).toBeInTheDocument();
	});
});
