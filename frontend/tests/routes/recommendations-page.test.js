/**
 * Tests for the recommendations route pull-request flow.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/svelte';
import RecommendationsPage from '../../src/routes/recommendations/+page.svelte';
import * as api from '$lib/api.js';

vi.mock('$lib/api.js', () => ({
	getActiveRecommendations: vi.fn(),
	getAppliedRecommendations: vi.fn(),
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
	getAutomationOperation: vi.fn(),
	approveAutomationPlan: vi.fn(),
	cancelAutomationPlan: vi.fn(),
	previewAutomationPlanRollback: vi.fn()
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

function actionReadyRecommendation(ids = [101]) {
	return {
		...recommendation,
		container_name: 'api',
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
		api.getActiveRecommendations.mockResolvedValue([recommendation]);
		api.getAppliedRecommendations.mockResolvedValue([]);
		api.getIgnoredRecommendations.mockResolvedValue([]);
		api.getOpenPullRequests.mockResolvedValue([]);
		api.getRecommendationSavings.mockResolvedValue({});
		api.createAutomationPlan.mockRejectedValue(planPreviewUnavailable());
		api.getAutomationPlan.mockResolvedValue(validatedPlan);
		api.getAutomationOperation.mockResolvedValue({
			operation_id: 'operation-1',
			status: 'running',
			result: { plan_status: 'applying' }
		});
		api.approveAutomationPlan.mockResolvedValue({ status: 'queued', operation_id: 'operation-1' });
		api.cancelAutomationPlan.mockResolvedValue({ ...validatedPlan, status: 'stale' });
		api.previewAutomationPlanRollback.mockResolvedValue({
			...validatedPlan,
			plan_id: 'pln-rollback',
			rollback_of_plan_id: 'pln-101'
		});
	});

	it('previews and approves a typed action using the server plan digest', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);

		render(RecommendationsPage);

		expect(await screen.findByText('Container action ready')).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Preview plan' }));

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
		await fireEvent.click(screen.getByRole('button', { name: 'Approve plan' }));

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
		const trigger = await screen.findByRole('button', { name: 'Preview plan' });
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
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));

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
			action: groupedAction
		};
		api.getActiveRecommendations.mockResolvedValue([apiRecommendation, sidecarRecommendation]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);

		render(RecommendationsPage);

		expect(await screen.findAllByText('Container action ready')).toHaveLength(2);
		await fireEvent.click(screen.getAllByRole('button', { name: 'Preview plan' })[0]);

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

		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));

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
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));

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
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));

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

		expect(await screen.findAllByRole('button', { name: 'Preview plan' })).toHaveLength(1);
	});

	it('refreshes an approved plan through merge, sync, and apply states', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce(validatedPlan);
		api.getAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			status: 'sync_pending'
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve plan' }));

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
		api.getAutomationOperation.mockResolvedValueOnce({
			operation_id: 'operation-1',
			status: 'running',
			result: { plan_status: 'applying' }
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));
		const approveButton = await screen.findByRole('button', { name: 'Approve plan' });

		vi.useFakeTimers();
		try {
			await fireEvent.click(approveButton);
			expect(screen.getByText(/Operation status: Queued/)).toBeInTheDocument();
			expect(screen.getByText(/Polling automatically/)).toBeInTheDocument();

			await act(async () => {
				await vi.advanceTimersByTimeAsync(3000);
			});

			expect(api.getAutomationOperation).toHaveBeenCalledWith('operation-1');
			expect(screen.getByText(/Operation status: Running/)).toBeInTheDocument();
			expect(screen.getByText('Plan lifecycle: Creating the pull request')).toBeInTheDocument();
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
		api.getAutomationOperation
			.mockRejectedValueOnce(new Error('worker credential hidden-secret'))
			.mockResolvedValueOnce({
				operation_id: 'operation-1',
				status: 'running',
				result: { plan_status: 'applying' }
			});

		render(RecommendationsPage);
			await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));
			const approveButton = await screen.findByRole('button', { name: 'Approve plan' });
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
			expect(api.getAutomationOperation).toHaveBeenCalledTimes(2);
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
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));
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
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));
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
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Approve plan' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Refresh plan status' }));

		expect(await screen.findByRole('button', { name: 'Preview rollback' })).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Preview rollback' }));

		expect(api.previewAutomationPlanRollback).toHaveBeenCalledWith('pln-101');
		expect(await screen.findByText('Rollback preview ready. Review it before approving.')).toBeInTheDocument();
		expect(screen.getByText('pln-101')).toBeInTheDocument();
		expect(screen.getByText('sha256:rollback-digest')).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Approve plan' })).toBeInTheDocument();
	});

	it('renders a safe generic warning without leaking internal renderer warning text', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation([101, 102])]);
		api.createAutomationPlan.mockResolvedValueOnce({
			...validatedPlan,
			warnings: ['render-warning']
		});

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));

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
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));

		expect(await screen.findByRole('alert')).toHaveTextContent(expectedMessage);
		expect(api.approveAutomationPlan).not.toHaveBeenCalled();
	});

	it('does not offer plan preview for an action without a safe container target', async () => {
		const unsafeRecommendation = actionReadyRecommendation();
		unsafeRecommendation.action.constraints.container_targeted = false;
		api.getActiveRecommendations.mockResolvedValue([unsafeRecommendation]);

		render(RecommendationsPage);

		await screen.findByText('CPU usage is consistently low');
		expect(screen.queryByRole('button', { name: 'Preview plan' })).not.toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Create pull request' })).not.toBeInTheDocument();
	});

	it('does not fall back to annotation-based PR handling when plan preparation is unavailable', async () => {
		api.getActiveRecommendations.mockResolvedValue([actionReadyRecommendation()]);
		api.createAutomationPlan.mockRejectedValueOnce(
			Object.assign(new Error('Server-side plan preparation is unavailable.'), {
				code: 'plan_preparation_unavailable'
			})
		);

		render(RecommendationsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Preview plan' }));

		expect(await screen.findByRole('alert')).toHaveTextContent(/server-side plan preparation is unavailable/i);
		expect(api.createAutomationPlan).toHaveBeenCalledWith([101]);
		expect(screen.queryByRole('button', { name: 'Create pull request' })).not.toBeInTheDocument();
	});
});
