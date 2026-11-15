/**
 * Tests for the recommendations route pull-request flow.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/svelte';
import RecommendationsPage from '../../src/routes/recommendations/+page.svelte';
import * as api from '$lib/api.js';
import { resetOptimizationRunsSupport } from '$lib/optimizationRuns.js';
import { currentUser, demoMode } from '$lib/stores.js';

vi.mock('$lib/api.js', () => ({
	discardAutomationPlan: vi.fn(),
	getNamespaces: vi.fn(),
	getOptimizationRun: vi.fn(),
	listOptimizationRuns: vi.fn(),
	requestOptimizationRun: vi.fn(),
	getActiveRecommendations: vi.fn(),
	getAppliedRecommendations: vi.fn(),
	getAutomationStatus: vi.fn(),
	getIgnoredRecommendations: vi.fn(),
	getOpenPullRequests: vi.fn(),
	getRecommendation: vi.fn(),
	getRecommendationEvents: vi.fn(),
	getRecommendationSavings: vi.fn(),
	getTopRecommendations: vi.fn(),
	ignoreRecommendation: vi.fn(),
	unignoreRecommendation: vi.fn(),
	createAutomationPlan: vi.fn(),
	getAutomationPlan: vi.fn(),
	approveAutomationPlan: vi.fn(),
	cancelAutomationPlan: vi.fn(),
	previewAutomationPlanRollback: vi.fn(),
	reportPlanDelivery: vi.fn(),
	planPatchUrl: vi.fn((id) => `/api/v1/automation/plans/${id}/patch`)
}));

const recommendation = {
	id: 101,
	type: 'RIGHTSIZING_CPU',
	scope: 'workload',
	owner_kind: 'Deployment',
	owner_name: 'api',
	namespace: 'production',
	pod_name: 'api-0',
	source: 'kubernetes',
	reason: 'CPU usage is consistently low'
};

function eligible(ids) {
	return { can_open_pr: true, delivery_modes: ['direct', 'manual'], blockers: [], warnings: [], recommendation_ids: ids };
}

function actionReadyRecommendation(ids = [101]) {
	return {
		...recommendation,
		container_name: 'api',
		eligibility: eligible(ids),
		action: {
			version: 3,
			action_id: 'action-101',
			recommendation_ids: ids,
			target: {
				api_version: 'apps/v1',
				kind: 'Deployment',
				namespace: 'production',
				name: 'api'
			},
			operations: [
				{
					op: 'set_container_resources',
					container: 'api',
					resources: { requests: { cpu: '500m' } }
				}
			],
			constraints: { container_targeted: true },
			evidence_ref: 'recommendation-evidence/101'
		}
	};
}

function planPreviewUnavailable() {
	return Object.assign(new Error('Server-side plan preparation is not configured.'), {
		status: 503,
		code: 'plan_preparation_unavailable'
	});
}

const validatedPlan = {
	plan_id: 'pln-101',
	recommendation_ids: [101, 102],
	status: 'validated',
	digest: 'sha256:exact-plan-digest',
	expires_at: '2099-10-21T23:00:00Z',
	source: {
		controller: 'argocd/payments-prod',
		repo: 'https://github.com/example/gitops.git',
		base_branch: 'main',
		base_revision: 'revision-101',
		path: 'apps/payments',
		renderer: 'raw'
	},
	files: [{ path: 'apps/payments/deployment.yaml', operation: 'update' }],
	diff: 'diff --git a/apps/payments/deployment.yaml',
	rendered_diff: 'Deployment/production/api container api changed',
	validation: {
		passed: true,
		target_changed: true,
		unrelated_changes: false,
		requests_within_limits: true,
		renderer: 'raw',
		output_digest: 'sha256:rendered-output-digest',
		warnings: ['The source revision is older than the latest observation.']
	},
	warnings: ['Review the rendered result before approving.']
};

describe('RecommendationsPage GitOps plan flow', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		resetOptimizationRunsSupport();
		api.discardAutomationPlan.mockResolvedValue({ discarded: true, status: 204 });
		api.getNamespaces.mockResolvedValue(['production']);
		api.listOptimizationRuns.mockResolvedValue([]);
		api.getActiveRecommendations.mockResolvedValue([recommendation]);
		api.getAppliedRecommendations.mockResolvedValue([]);
		api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true, mode: 'inline' });
		api.getIgnoredRecommendations.mockResolvedValue([]);
		api.getOpenPullRequests.mockResolvedValue([]);
		api.getRecommendationSavings.mockResolvedValue({});
		api.createAutomationPlan.mockRejectedValue(planPreviewUnavailable());
		api.getAutomationPlan.mockResolvedValue(validatedPlan);
		api.approveAutomationPlan.mockResolvedValue({ status: 'queued', operation_id: 'operation-1' });
		api.cancelAutomationPlan.mockResolvedValue({ ...validatedPlan, status: 'stale' });
		api.previewAutomationPlanRollback.mockResolvedValue({
			...validatedPlan,
			plan_id: 'pln-rollback',
			rollback_of_plan_id: 'pln-101'
		});
	});

	it('keeps recommendations visible but disables PR previews when automation is off', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.getAutomationStatus.mockResolvedValue({ enabled: false, ready: false, mode: 'disabled' });

		render(RecommendationsPage);

		expect(
			await screen.findByText(
				'GitOps automation is disabled. Recommendations remain visible, but pull-request previews are unavailable; apply changes manually.'
			)
		).toBeInTheDocument();
		expect(await screen.findByText('CPU usage is consistently low')).toBeInTheDocument();
		expect(screen.getByText('Manual change required')).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Open PR' })).not.toBeInTheDocument();
	});

	it('shows the allocated saving with the fixed-nodes note and the projected released capacity (IMP-03, WP-08.1)', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			{
				...actionReadyRecommendation(),
				potential_savings_cost: 0,
				potential_savings_co2e_grams: 0,
				projected_allocated_cost_annual: 236.52,
				projected_billable_cost_annual: 0,
				projection_note: 'fixed nodes: frees capacity, bill unchanged until nodes are removed',
				released_capacity: { method: 'projected', instantaneous: { cpu_cores: 0.9, replicas: 1 }, direction: 'release' }
			}
		]);
		render(RecommendationsPage);
		const allocated = await screen.findByTestId('row-allocated-saving');
		expect(allocated).toHaveTextContent('allocated:');
		expect(allocated).toHaveTextContent('fixed nodes: frees capacity, bill unchanged until nodes are removed');
		expect(screen.getByTestId('row-released-capacity')).toHaveTextContent('Frees 0.90 cores (projected)');
	});

	it('shows readiness as pending instead of disabled while the status request is unresolved', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		let resolveStatus;
		api.getAutomationStatus.mockReturnValue(
			new Promise((resolve) => {
				resolveStatus = resolve;
			})
		);

		render(RecommendationsPage);

		expect(await screen.findByText('Checking GitOps automation readiness…')).toBeInTheDocument();
		expect(
			screen.queryByText(
				'GitOps automation is disabled. Recommendations remain visible, but pull-request previews are unavailable; apply changes manually.'
			)
		).not.toBeInTheDocument();

		await act(async () => {
			resolveStatus({ enabled: false, ready: false, mode: 'disabled' });
		});

		expect(
			await screen.findByText(
				'GitOps automation is disabled. Recommendations remain visible, but pull-request previews are unavailable; apply changes manually.'
			)
		).toBeInTheDocument();
	});

	it('disables PR previews and reports an automation status failure without hiding recommendations', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.getAutomationStatus.mockRejectedValue(new Error('status endpoint unavailable'));

		render(RecommendationsPage);

		expect(
			await screen.findByText(/GitOps automation status is unavailable\. Pull-request previews are disabled/)
		).toBeInTheDocument();
		expect(await screen.findByText('CPU usage is consistently low')).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Open PR' })).not.toBeInTheDocument();
	});

	it('previews and approves a typed action using the server plan digest', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);

		render(RecommendationsPage);

		expect(await screen.findByText('Container action ready')).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Open PR' }));

		expect(api.createAutomationPlan).toHaveBeenCalledWith([101, 102]);
		expect(await screen.findByText('Review automation plan')).toBeInTheDocument();
		expect(screen.getByText('argocd/payments-prod')).toBeInTheDocument();
		expect(screen.getByText('https://github.com/example/gitops.git')).toBeInTheDocument();
		expect(screen.getByText('apps/payments')).toBeInTheDocument();
		expect(screen.getByText('raw')).toBeInTheDocument();
		expect(screen.getByText('revision-101')).toBeInTheDocument();
		expect(screen.getByText('apps/payments/deployment.yaml')).toBeInTheDocument();
		expect(screen.getByText('update')).toBeInTheDocument();
		expect(screen.getByText('diff --git a/apps/payments/deployment.yaml')).toBeInTheDocument();
		expect(screen.getByText('Deployment/production/api container api changed')).toBeInTheDocument();
		expect(screen.getByText('Validation passed')).toBeInTheDocument();
		expect(screen.getByText('Review the rendered result before approving.')).toBeInTheDocument();
		expect(
			screen.getByText('The source revision is older than the latest observation.')
		).toBeInTheDocument();
		expect(screen.getByText('sha256:rendered-output-digest')).toBeInTheDocument();
		expect(screen.getByText('sha256:exact-plan-digest')).toBeInTheDocument();
		expect(screen.getByText('2099-10-21T23:00:00Z')).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Approve and open PR' }));

		expect(api.approveAutomationPlan).toHaveBeenCalledWith(
			'pln-101',
			'sha256:exact-plan-digest'
		);
		expect(await screen.findByText(/Plan approved: queued/)).toBeInTheDocument();
	});

	it('traps keyboard focus in the plan dialog and restores focus when it closes', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);

		render(RecommendationsPage);
		const trigger = await screen.findByRole('button', { name: 'Open PR' });
		trigger.focus();
		await fireEvent.click(trigger);

		const dialog = await screen.findByRole('dialog', { name: 'Review automation plan' });
		const focusableElements = dialog.querySelectorAll(
			'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
		);
		const first = focusableElements[0];
		const last = focusableElements[focusableElements.length - 1];
		expect(focusableElements.length).toBeGreaterThan(1);
		expect(document.activeElement).toBe(first);

		last.focus();
		await fireEvent.keyDown(last, { key: 'Tab' });
		expect(document.activeElement).toBe(first);

		first.focus();
		await fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
		expect(document.activeElement).toBe(last);

		await fireEvent.click(screen.getByRole('button', { name: 'Close pull request dialog' }));
		expect(screen.queryByRole('dialog', { name: 'Review automation plan' })).not.toBeInTheDocument();
		expect(document.activeElement).toBe(trigger);
	});

	it('offers a one-time server-validated Helm mapping when inference is ambiguous', async () => {
		const mappingOptions = {
			files: ['apps/payments/values.yaml'],
			containers: ['api']
		};
		const mappingError = Object.assign(new Error('Mapping required.'), {
			code: 'needs_chart_mapping',
			mappingOptions
		});
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan
			.mockRejectedValueOnce(mappingError)
			.mockResolvedValueOnce(validatedPlan);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByRole('heading', { name: 'One-time Helm values mapping' })).toBeInTheDocument();
		expect(screen.getByLabelText('Git-managed values file')).toHaveValue('apps/payments/values.yaml');
		expect(screen.getByLabelText('Container')).toHaveValue('api');
		await fireEvent.input(screen.getByLabelText('Resources key path'), {
			target: { value: 'app.resources' }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Validate mapping and preview' }));

		expect(api.createAutomationPlan).toHaveBeenNthCalledWith(1, [101]);
		expect(api.createAutomationPlan).toHaveBeenNthCalledWith(2, [101], {
			file_path: 'apps/payments/values.yaml',
			container: 'api',
			path: ['app', 'resources']
		});
		expect(await screen.findByText('diff --git a/apps/payments/deployment.yaml')).toBeInTheDocument();
		expect(screen.getByText('One-time mapping validated. Review the plan before approving.')).toBeInTheDocument();
		expect(screen.queryByRole('heading', { name: 'One-time Helm values mapping' })).not.toBeInTheDocument();
	});

	it('previews a single change set containing multiple container operations', async () => {
		const apiRecommendation = actionReadyRecommendation([101, 102]);
		const groupedAction = {
			...apiRecommendation.action,
			operations: [
				...apiRecommendation.action.operations,
				{
					op: 'set_container_resources',
					container: 'sidecar',
					resources: { requests: { memory: '64Mi' } }
				}
			]
		};
		apiRecommendation.action = groupedAction;
		const sidecarRecommendation = {
			...recommendation,
			id: 102,
			container_name: 'sidecar',
			action: groupedAction,
			eligibility: eligible([101, 102])
		};
		api.getActiveRecommendations.mockResolvedValue([apiRecommendation, sidecarRecommendation]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);

		render(RecommendationsPage);

		expect(await screen.findAllByText('Container action ready')).toHaveLength(2);
		await fireEvent.click(screen.getAllByRole('button', { name: 'Open PR' })[0]);

		expect(api.createAutomationPlan).toHaveBeenCalledWith([101, 102]);
		expect(await screen.findByText('Review automation plan')).toBeInTheDocument();
	});

	it('surfaces drift between the planned branch head and controller revision', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			source: {
				...validatedPlan.source,
				source_revision: 'b'.repeat(40)
			}
		});

		render(RecommendationsPage);

		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByText(/current repository branch head.*differs from the revision reported/i)).toBeInTheDocument();
		expect(screen.getByText('Controller-reported revision')).toBeInTheDocument();
		expect(screen.getByText('Branch revision used by plan')).toBeInTheDocument();
	});

	it('shows persisted policy checks for an approved preview', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			policy_result: {
				allowed: true,
				checks: [
					{
						gate: 'sample_count_coverage',
						state: 'passed',
						message: 'Recommendation sample count and coverage meet the configured minimums.'
					}
				],
				rejections: []
			}
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByText('Automation policy')).toBeInTheDocument();
		expect(screen.getByText('Policy checks passed')).toBeInTheDocument();
		expect(screen.getByText('sample count coverage')).toBeInTheDocument();
		expect(
			screen.getByText('Recommendation sample count and coverage meet the configured minimums.')
		).toBeInTheDocument();
	});

	it('shows policy rejection details when the API blocks plan creation', async () => {
		const rejection = Object.assign(new Error('Confidence is below the configured minimum.'), {
			code: 'policy_confidence_too_low',
			policyResult: {
				allowed: false,
				checks: [
					{
						gate: 'minimum_confidence',
						state: 'rejected',
						message: 'Recommendation confidence is below the configured minimum.'
					}
				],
				rejections: [
					{
						code: 'policy_confidence_too_low',
						detail: 'Collect stronger evidence before retrying.'
					}
				]
			}
		});
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockRejectedValueOnce(rejection);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByText('Policy blocks this plan')).toBeInTheDocument();
		expect(screen.getByText('minimum confidence')).toBeInTheDocument();
		expect(screen.getByText('Collect stronger evidence before retrying.')).toBeInTheDocument();
	});

	it('keeps open-PR identity scoped to the recommendation container', async () => {
		const firstContainer = actionReadyRecommendation();
		const secondContainer = actionReadyRecommendation([102]);
		secondContainer.id = 102;
		secondContainer.container_name = 'sidecar';
		secondContainer.action.action_id = 'action-102';
		secondContainer.action.operations[0].container = 'sidecar';
		api.getActiveRecommendations.mockResolvedValue([firstContainer, secondContainer]);
		api.getOpenPullRequests.mockResolvedValue([{ recommendation_id: 101 }]);

		render(RecommendationsPage);

		expect(await screen.findAllByRole('button', { name: 'Open PR' })).toHaveLength(1);
	});

	it('refreshes an approved plan through merge, sync, and apply states', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		api.getAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			status: 'sync_pending'
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve and open PR' }));

		await fireEvent.click(await screen.findByRole('button', { name: 'Refresh plan status' }));

		expect(api.getAutomationPlan).toHaveBeenCalledWith('pln-101');
		expect(await screen.findByText('Plan lifecycle: Waiting for workload apply')).toBeInTheDocument();
	});

	it('shows queued operation status and indicates automatic status polling', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		api.approveAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			status: 'approved',
			operation_id: 'operation-1',
			operation_status: 'queued'
		});
		api.getAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			status: 'applying',
			operation_id: 'operation-1',
			operation_status: 'running'
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		const approveButton = await screen.findByRole('button', { name: 'Approve and open PR' });

		vi.useFakeTimers();
		try {
			await fireEvent.click(approveButton);
			expect(screen.getByText(/Operation status: Queued/)).toBeInTheDocument();
			expect(screen.getByText(/Polling automatically/)).toBeInTheDocument();

			await act(async () => {
				await vi.advanceTimersByTimeAsync(3000);
			});

			expect(api.getAutomationPlan).toHaveBeenCalledWith('pln-101');
			expect(screen.getByText(/Operation status: Running/)).toBeInTheDocument();
			expect(screen.getByText('Plan lifecycle: Creating the pull request')).toBeInTheDocument();
		} finally {
			await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
			vi.clearAllTimers();
			vi.useRealTimers();
		}
	});

	it('refreshes the pull-request list when an operation opens a PR', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		api.approveAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			status: 'approved',
			operation_id: 'operation-1',
			operation_status: 'queued'
		});
		api.getAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			status: 'pr_open',
			operation_id: 'operation-1',
			operation_status: 'succeeded'
		});
		api.getOpenPullRequests
			.mockResolvedValueOnce([])
			.mockResolvedValueOnce([{ recommendation_id: 101, status: 'open' }]);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		const approveButton = await screen.findByRole('button', { name: 'Approve and open PR' });

		vi.useFakeTimers();
		try {
			await fireEvent.click(approveButton);
			await act(async () => {
				await vi.advanceTimersByTimeAsync(3000);
			});

			expect(api.getOpenPullRequests).toHaveBeenCalledTimes(2);
			// The open pull requests live on the changes board (K4): the tab is a link there.
			const changesLink = screen.getByTestId('changes-link');
			expect(changesLink).toHaveAttribute('href', '/changes');
			expect(changesLink).toHaveTextContent('Pull requests 1');
			expect(screen.queryByRole('button', { name: /Pull requests/ })).not.toBeInTheDocument();
		} finally {
			await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
			vi.clearAllTimers();
			vi.useRealTimers();
		}
	});

	it('shows a safe polling failure and retries operation status', async () => {
		const approvedPlan = {
			...validatedPlan,
			status: 'approved',
			operation_id: 'operation-1',
			operation_status: 'queued'
		};
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		api.approveAutomationPlan.mockResolvedValueOnce(approvedPlan);
		api.getAutomationPlan
			.mockRejectedValueOnce(new Error('worker credential hidden-secret'))
			.mockResolvedValueOnce({
				...validatedPlan,
				status: 'applying',
				operation_id: 'operation-1',
				operation_status: 'running'
			});

		render(RecommendationsPage);
			await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
			const approveButton = await screen.findByRole('button', { name: 'Approve and open PR' });
			vi.useFakeTimers();
			try {
				await fireEvent.click(approveButton);

			await act(async () => {
				await vi.advanceTimersByTimeAsync(3000);
			});
			expect(
				screen.getByText('Operation status could not be refreshed; automatic polling will retry.')
			).toBeInTheDocument();
			expect(screen.queryByText(/hidden-secret/)).not.toBeInTheDocument();

			await act(async () => {
				await vi.advanceTimersByTimeAsync(3000);
			});
			expect(screen.getByText(/Operation status: Running/)).toBeInTheDocument();
			expect(
				screen.queryByText('Operation status could not be refreshed; automatic polling will retry.')
			).not.toBeInTheDocument();
			expect(api.getAutomationPlan).toHaveBeenCalledTimes(2);
		} finally {
			await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
			vi.clearAllTimers();
			vi.useRealTimers();
		}
	});

	it('cancels an eligible plan and allows a fresh preview', async () => {
		const freshPlan = {
			...validatedPlan,
			plan_id: 'pln-102',
			digest: 'sha256:fresh-plan-digest'
		};
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan
			.mockResolvedValueOnce(validatedPlan)
			.mockResolvedValueOnce(freshPlan);
		api.cancelAutomationPlan.mockResolvedValueOnce({ ...validatedPlan, status: 'stale' });

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Cancel plan' }));

		expect(api.cancelAutomationPlan).toHaveBeenCalledWith('pln-101');
		expect(await screen.findByText('Plan cancelled. Preview a new plan before approving.')).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Re-plan' }));

		expect(api.createAutomationPlan).toHaveBeenCalledTimes(2);
		expect(await screen.findByText('sha256:fresh-plan-digest')).toBeInTheDocument();
		expect(screen.getByText('New plan preview ready. Review it before approving.')).toBeInTheDocument();
	});

	it('shows a cancellation failure without losing the plan preview', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		api.cancelAutomationPlan.mockRejectedValueOnce(new Error('Cancellation was rejected.'));

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Cancel plan' }));

		expect(await screen.findByRole('alert')).toHaveTextContent('Cancellation was rejected.');
		expect(screen.getByText('sha256:exact-plan-digest')).toBeInTheDocument();
		expect(api.cancelAutomationPlan).toHaveBeenCalledWith('pln-101');
	});

	it('offers a rollback preview for a synced plan and returns to the approval flow', async () => {
		const approvedPlan = {
			...validatedPlan,
			status: 'approved',
			operation_id: 'operation-1',
			operation_status: 'succeeded'
		};
		const syncedPlan = { ...approvedPlan, status: 'synced' };
		const rollbackPreview = {
			...validatedPlan,
			plan_id: 'pln-rollback',
			rollback_of_plan_id: 'pln-101',
			digest: 'sha256:rollback-digest'
		};
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		api.approveAutomationPlan.mockResolvedValueOnce(approvedPlan);
		api.getAutomationPlan.mockResolvedValueOnce(syncedPlan);
		api.previewAutomationPlanRollback.mockResolvedValueOnce(rollbackPreview);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve and open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Refresh plan status' }));

		expect(await screen.findByRole('button', { name: 'Preview rollback' })).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Preview rollback' }));

		expect(api.previewAutomationPlanRollback).toHaveBeenCalledWith('pln-101');
		expect(await screen.findByText('Rollback preview ready. Review it before approving.')).toBeInTheDocument();
		expect(screen.getByText('pln-101')).toBeInTheDocument();
		expect(screen.getByText('sha256:rollback-digest')).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Approve and open PR' })).toBeInTheDocument();
	});

	it('renders a safe generic warning without leaking internal renderer warning text', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			warnings: ['render-warning']
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(
			await screen.findByText('Renderer reported a non-blocking warning; details are omitted for safety.')
		).toBeInTheDocument();
		expect(screen.queryByText('render-warning')).not.toBeInTheDocument();
	});

	it.each([
		['unsupported_source', /source.*not supported/i],
		['needs_chart_mapping', /one-time values mapping.*Git-managed values file/i],
		['ambiguous_binding', /binding.*ambiguous/i],
		['plan_stale', /plan.*stale/i],
		['provider_not_ready', /provider.*unavailable/i]
	])('renders an explicit %s plan-preview error', async (code, expectedMessage) => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockRejectedValueOnce(Object.assign(new Error('Request failed.'), { code }));

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByRole('alert')).toHaveTextContent(expectedMessage);
		expect(api.approveAutomationPlan).not.toHaveBeenCalled();
	});

	it('does not offer plan preview when the server reports a blocker, and shows its fix (F10)', async () => {
		const unsafeRecommendation = actionReadyRecommendation();
		unsafeRecommendation.eligibility = {
			can_open_pr: false,
			delivery_modes: ['manual'],
			blockers: [
				{
					code: 'requires_container_target',
					message: 'The proposed change does not target one named container of the workload.',
					fix: 'Rescan once the container metrics are collected.'
				}
			],
			warnings: [],
			recommendation_ids: null
		};
		api.getActiveRecommendations.mockResolvedValue([unsafeRecommendation]);

		render(RecommendationsPage);

		await screen.findByText('CPU usage is consistently low');
		expect(screen.queryByRole('button', { name: 'Open PR' })).not.toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Create pull request' })).not.toBeInTheDocument();
		const blockers = screen.getByTestId('eligibility-blockers');
		expect(blockers).toHaveTextContent('The proposed change does not target one named container of the workload.');
		expect(blockers).toHaveTextContent('Rescan once the container metrics are collected.');
		expect(screen.getByText('Manual change required')).toHaveAttribute(
			'title',
			'The proposed change does not target one named container of the workload. Rescan once the container metrics are collected.'
		);
	});

	it('replaces per-card automation diagnostics with one demo notice in demo mode only', async () => {
		const demoRecommendation = actionReadyRecommendation();
		demoRecommendation.eligibility = {
			can_open_pr: false,
			delivery_modes: ['manual'],
			blockers: [
				{ code: 'action_unavailable', message: 'No typed container-resources change could be built.' },
				{ code: 'automation_not_configured', message: 'The automation plan service is not available.' }
			],
			warnings: [],
			recommendation_ids: null
		};
		api.getActiveRecommendations.mockResolvedValue([demoRecommendation]);
		demoMode.set(true);
		try {
			render(RecommendationsPage);
			await screen.findByText('CPU usage is consistently low');
			expect(screen.getByTestId('demo-notice')).toHaveTextContent('pull requests are disabled in the demo');
			expect(screen.queryByTestId('eligibility-blockers')).not.toBeInTheDocument();
		} finally {
			demoMode.set(false);
		}
	});

	it('keeps the automation diagnostics on a real install', async () => {
		const realRecommendation = actionReadyRecommendation();
		realRecommendation.eligibility = {
			can_open_pr: false,
			delivery_modes: ['manual'],
			blockers: [{ code: 'automation_not_configured', message: 'The automation plan service is not available.' }],
			warnings: [],
			recommendation_ids: null
		};
		api.getActiveRecommendations.mockResolvedValue([realRecommendation]);

		render(RecommendationsPage);
		await screen.findByText('CPU usage is consistently low');
		expect(screen.queryByTestId('demo-notice')).not.toBeInTheDocument();
		expect(screen.getByTestId('eligibility-blockers')).toHaveTextContent('The automation plan service is not available.');
	});

	it('trusts the server eligibility instead of re-checking the action client-side', async () => {
		// The typed action is not even returned: the server decision is authoritative.
		const serverEligible = { ...recommendation, id: 101, action: undefined, eligibility: eligible([101]) };
		api.getActiveRecommendations.mockResolvedValue([serverEligible]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(api.createAutomationPlan).toHaveBeenCalledWith([101]);
	});

	it('shows a warming-up blocker with its ETA', async () => {
		api.getActiveRecommendations.mockResolvedValue([
			{
				...recommendation,
				eligibility: {
					can_open_pr: false,
					delivery_modes: ['manual'],
					blockers: [
						{
							code: 'warming_up',
							message: 'Collecting evidence: 12/36 samples, 17% coverage (minimum 50%).',
							fix: 'Eligible in ~31h; no action is needed.'
						}
					],
					warnings: [{ code: 'singleton_workload', message: 'The workload runs a single replica.', fix: '' }],
					eligible_at: '2099-01-01T00:00:00+00:00'
				}
			}
		]);

		render(RecommendationsPage);

		const blockers = await screen.findByTestId('eligibility-blockers');
		expect(blockers).toHaveTextContent('Collecting evidence: 12/36 samples');
		expect(blockers).toHaveTextContent('Eligible in ~31h');
		expect(blockers).toHaveTextContent('The workload runs a single replica.');
	});

	it('does not fall back to annotation-based PR handling when plan preparation is unavailable', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockRejectedValueOnce(
			Object.assign(new Error('Server-side plan preparation is unavailable.'), {
				code: 'plan_preparation_unavailable'
			})
		);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByRole('alert')).toHaveTextContent(/server-side plan preparation is unavailable/i);
		expect(api.createAutomationPlan).toHaveBeenCalledWith([101]);
		expect(screen.queryByRole('button', { name: 'Create pull request' })).not.toBeInTheDocument();
	});

	it('shows an applied change as baseline to live value, with the proposal when it differs', async () => {
		api.getAppliedRecommendations.mockResolvedValue([
			{
				...recommendation,
				id: 42,
				pod_name: 'oversized-web',
				status: 'verified',
				verification_status: 'passed',
				application_method: 'pr_merge',
				applied_at: '2026-11-08T11:58:13+00:00',
				created_at: '2026-11-08T11:52:59+00:00',
				current_cpu_request_millicores: 400,
				recommended_cpu_request_millicores: 10,
				actual_cpu_request_millicores: 12
			}
		]);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: /Measured savings/ }));
		await fireEvent.click(await screen.findByText('oversized-web'));

		const change = await screen.findByTestId('request-change-cpu');
		expect(change).toHaveTextContent('400m');
		expect(change).toHaveTextContent('12m');
		expect(change).toHaveTextContent('(live after the change)');
		expect(change).toHaveTextContent('proposed 10m');
		expect(change).not.toHaveTextContent('live request unchanged');
	});

	it('flags an applied change whose live request still equals the baseline instead of showing 400m → 400m', async () => {
		api.getAppliedRecommendations.mockResolvedValue([
			{
				...recommendation,
				id: 44,
				type: 'RIGHTSIZING_MEMORY',
				pod_name: 'oversized-web',
				status: 'applied',
				applied_at: '2026-11-08T11:58:13+00:00',
				created_at: '2026-11-08T11:52:59+00:00',
				current_memory_request_bytes: 402653184,
				recommended_memory_request_bytes: 16777216,
				actual_memory_request_bytes: 402653184
			}
		]);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: /Measured savings/ }));
		await fireEvent.click(await screen.findByText('oversized-web'));

		const change = await screen.findByTestId('request-change-memory');
		expect(change).toHaveTextContent('384.0 MB');
		expect(change).toHaveTextContent('16.0 MB');
		expect(change).toHaveTextContent('(proposed)');
		expect(change).toHaveTextContent('live request unchanged (384.0 MB)');
		expect(change).not.toHaveTextContent('(live after the change)');
	});

	it('shows a merged change with an unknown live value as baseline to proposal, never as unchanged', async () => {
		// Migration 0061 clears the actual_* value that old rescans reverted to the baseline.
		api.getAppliedRecommendations.mockResolvedValue([
			{
				...recommendation,
				id: 42,
				pod_name: 'oversized-web',
				status: 'verified',
				application_method: 'pr_merge',
				applied_at: '2026-11-08T11:58:13+00:00',
				created_at: '2026-11-08T11:52:59+00:00',
				current_cpu_request_millicores: 400,
				recommended_cpu_request_millicores: 10,
				actual_cpu_request_millicores: null
			}
		]);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: /Measured savings/ }));
		const summary = await screen.findByTestId('request-summary-cpu');
		expect(summary).toHaveTextContent('400m → 10m (proposed)');

		await fireEvent.click(await screen.findByText('oversized-web'));
		const change = await screen.findByTestId('request-change-cpu');
		expect(change).toHaveTextContent('400m');
		expect(change).toHaveTextContent('10m');
		expect(change).toHaveTextContent('(proposed)');
		expect(change).not.toHaveTextContent('live request unchanged');
	});
});

describe('RecommendationsPage one-click Open PR, delivery modes and warm-up (F4, F5, F7)', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		resetOptimizationRunsSupport();
		api.discardAutomationPlan.mockResolvedValue({ discarded: true, status: 204 });
		api.getNamespaces.mockResolvedValue(['production']);
		api.listOptimizationRuns.mockResolvedValue([]);
		api.getAppliedRecommendations.mockResolvedValue([]);
		api.getIgnoredRecommendations.mockResolvedValue([]);
		api.getOpenPullRequests.mockResolvedValue([]);
		api.getRecommendationSavings.mockResolvedValue({});
		api.getAutomationPlan.mockResolvedValue(validatedPlan);
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
	});

	it('skips the approval click when the autoApprove policy approved the preview', async () => {
		api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true, delivery_mode: 'direct' });
		api.createAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			status: 'approved',
			operation_id: 'operation-1',
			operation_status: 'queued',
			delivery_mode: 'direct',
			auto_approved: true,
			auto_approve_blockers: []
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByText('Approved automatically by the automation.autoApprove policy.')).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Approve and open PR' })).not.toBeInTheDocument();
		expect(api.approveAutomationPlan).not.toHaveBeenCalled();
	});

	it('explains why a contested preview still needs the click', async () => {
		api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true, delivery_mode: 'direct' });
		api.createAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			auto_approved: false,
			auto_approve_blockers: ['consensus_contested']
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByTestId('auto-approve-note')).toHaveTextContent(/sources disagree/);
		expect(screen.getByRole('button', { name: 'Approve and open PR' })).toBeInTheDocument();
	});

	it('offers the patch and the "I opened PR" link-back in patch mode', async () => {
		api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true, delivery_mode: 'patch' });
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		const approved = { ...validatedPlan, status: 'approved', delivery_mode: 'patch' };
		api.approveAutomationPlan.mockResolvedValueOnce(approved);
		api.getAutomationPlan.mockResolvedValue(approved);
		api.reportPlanDelivery.mockResolvedValueOnce({
			plan: { ...approved, status: 'pr_open' },
			pull_request: { number: 42, url: 'https://github.com/example/gitops/pull/42', linked_by: 'trailer' },
			created: true
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve and get the patch' }));

		const delivery = await screen.findByTestId('external-delivery');
		expect(screen.getByRole('link', { name: 'Download the patch' })).toHaveAttribute(
			'href',
			'/api/v1/automation/plans/pln-101/patch'
		);
		expect(delivery).toHaveTextContent('greenkube plan apply-patch pln-101');
		await fireEvent.input(screen.getByPlaceholderText('https://github.com/org/repo/pull/42'), {
			target: { value: 'https://github.com/example/gitops/pull/42' }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Link pull request' }));

		expect(api.reportPlanDelivery).toHaveBeenCalledWith('pln-101', 'https://github.com/example/gitops/pull/42');
		expect(await screen.findByText(/Pull request #42 linked/)).toBeInTheDocument();
	});

	it('approves for CI delivery in ci mode', async () => {
		api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true, delivery_mode: 'ci' });
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		const approved = { ...validatedPlan, status: 'approved', delivery_mode: 'ci' };
		api.approveAutomationPlan.mockResolvedValueOnce(approved);
		api.getAutomationPlan.mockResolvedValue(approved);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve for CI delivery' }));

		expect(await screen.findByText(/Approved for CI delivery/)).toBeInTheDocument();
	});

	it('shows when a warming-up recommendation becomes eligible', async () => {
		api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true });
		const eligibleAt = new Date(Date.now() + 31 * 3_600_000 + 60_000).toISOString();
		api.getActiveRecommendations.mockResolvedValue([
			{
				...recommendation,
				eligibility: {
					can_open_pr: false,
					delivery_modes: ['manual'],
					blockers: [{ code: 'warming_up', message: 'Collecting usage evidence.', fix: 'Wait.' }],
					warnings: [],
					eligible_at: eligibleAt
				}
			}
		]);

		render(RecommendationsPage);

		expect(await screen.findByTestId('warm-up-eta')).toHaveTextContent('Collecting evidence — eligible in ~31h');
	});
});

describe('RecommendationsPage role gating (viewer, write without approve)', () => {
	const VIEWER = { kind: 'user', name: 'vera', role: 'viewer', scopes: ['read', 'metrics:read'] };
	const WRITER_KEY = { kind: 'apikey', name: 'ci', role: null, scopes: ['read', 'write'] };

	beforeEach(() => {
		vi.clearAllMocks();
		resetOptimizationRunsSupport();
		api.discardAutomationPlan.mockResolvedValue({ discarded: true, status: 204 });
		api.getNamespaces.mockResolvedValue(['production']);
		api.listOptimizationRuns.mockResolvedValue([]);
		api.getAppliedRecommendations.mockResolvedValue([]);
		api.getIgnoredRecommendations.mockResolvedValue([{ ...recommendation, id: 202, pod_name: 'ignored-0', status: 'ignored' }]);
		api.getOpenPullRequests.mockResolvedValue([]);
		api.getRecommendationSavings.mockResolvedValue({});
		api.getAutomationStatus.mockResolvedValue({ enabled: true, ready: true, delivery_mode: 'direct' });
		api.getAutomationPlan.mockResolvedValue(validatedPlan);
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101])]);
	});

	it('hides Open PR, Ignore, Restore and Rescan from a viewer and explains why', async () => {
		currentUser.set(VIEWER);
		render(RecommendationsPage);

		expect(await screen.findByTestId('read-only-hint')).toHaveTextContent('Read-only access (viewer)');
		expect(screen.queryByRole('button', { name: 'Open PR' })).not.toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Ignore' })).not.toBeInTheDocument();
		expect(screen.queryByRole('button', { name: /Rescan/ })).not.toBeInTheDocument();

		await fireEvent.click(screen.getByRole('button', { name: /^Ignored/ }));
		expect(await screen.findByText('ignored-0')).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: /Restore/ })).not.toBeInTheDocument();
	});

	it('shows the actions to an operator without the read-only hint', async () => {
		render(RecommendationsPage);

		expect(await screen.findByRole('button', { name: 'Open PR' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Ignore' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: /Rescan/ })).toBeInTheDocument();
		expect(screen.queryByTestId('read-only-hint')).not.toBeInTheDocument();
	});

	it('does not offer the approval to a principal without the approve scope', async () => {
		currentUser.set(WRITER_KEY);
		api.createAutomationPlan.mockResolvedValueOnce({ ...validatedPlan, auto_approved: false, auto_approve_blockers: [] });
		render(RecommendationsPage);

		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByTestId('approve-read-only')).toHaveTextContent('approve scope');
		expect(screen.queryByRole('button', { name: 'Approve and open PR' })).not.toBeInTheDocument();
	});

	it('honours a plan that says the requester may not approve it', async () => {
		api.createAutomationPlan.mockResolvedValueOnce({ ...validatedPlan, may_approve: false });
		render(RecommendationsPage);

		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		expect(await screen.findByTestId('approve-read-only')).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Approve and open PR' })).not.toBeInTheDocument();
	});

	it('labels the increase auto-approval blockers', async () => {
		api.createAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			auto_approved: false,
			auto_approve_blockers: ['increase_without_native_evidence', 'increase_requires_approval']
		});
		render(RecommendationsPage);

		await fireEvent.click(await screen.findByRole('button', { name: 'Open PR' }));

		const note = await screen.findByTestId('auto-approve-note');
		expect(note).toHaveTextContent("an increase is only approved automatically when GreenKube's own analyzer sized it");
		expect(note).toHaveTextContent('request increases need automation.autoApprove.increases');
		expect(note).not.toHaveTextContent('increase_without_native_evidence');
	});
});
