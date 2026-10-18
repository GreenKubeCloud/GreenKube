/**
 * Tests for GitOps discovery, manual binding resolution, and automation operations.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import AutomationPage from '../../src/routes/automation/+page.svelte';
import * as api from '$lib/api.js';

vi.mock('$lib/api.js', () => ({
	getGitOpsStatus: vi.fn(),
	getGitOpsCoverage: vi.fn(),
	getGitOpsBindings: vi.fn(),
	resolveGitOpsBinding: vi.fn(),
	refreshGitOpsDiscovery: vi.fn(),
	getAutomationStatus: vi.fn(),
	getOpenPullRequests: vi.fn()
}));

const gitOpsStatus = {
	cluster: 'cluster-with-private-data',
	discovery: {
		argocd: { available: true, reason: null },
		flux_kustomization: { available: false, reason: 'crd-not-found' },
		flux_helm_release: { available: true, reason: null },
		flux_source_git_repository: { available: true, reason: null },
		flux_source_oci_repository: { available: false, reason: 'unsafe provider detail' },
		flux_source_helm_repository: { available: false, reason: null },
		flux_source_bucket: { available: false, reason: null }
	},
	last_refresh: '2026-10-22T00:00:00Z',
	coverage: { total: 10, resolved: 8, ambiguous: 1, unresolved: 1, blocked: 0, stale: 2, ratio: 0.8 }
};

const coverage = {
	total: 10,
	resolved: 8,
	ambiguous: 1,
	unresolved: 1,
	blocked: 0,
	stale: 2,
	ratio: 0.8
};

const actionableBinding = {
	cluster: 'private-cluster-name',
	namespace: 'payments',
	workload_kind: 'Deployment',
	workload_name: 'api',
	status: 'ambiguous',
	reason: 'conflicting-equal-priority-source-units',
	candidates: [
		{
			id: 'argocd-123456789012345678901234',
			generation_token: `sha256:${'a'.repeat(64)}`,
			controller_kind: 'argocd',
			controller: 'argocd/payments-prod',
			controller_name: 'payments-prod',
			controller_namespace: 'argocd',
			provider_id: 'production-github',
			repo_url: 'https://github.example.test/acme/platform-config.git',
			path: 'apps/payments',
			renderer: 'raw',
			status: 'resolved',
			reason: null,
			revision: 'a'.repeat(40),
			source_revision: 'b'.repeat(40),
			observed_at: '2026-10-22T00:00:00Z'
		},
		{
			id: 'flux-123456789012345678901234',
			generation_token: `sha256:${'b'.repeat(64)}`,
			controller_kind: 'flux_kustomization',
			controller: 'flux_kustomization/payments',
			controller_name: 'payments',
			controller_namespace: 'flux-system',
			provider_id: 'production-github',
			repo_url: 'https://user:credential@github.example.test/acme/other.git?token=secret',
			path: 'apps/payments',
			renderer: 'kustomize',
			status: 'ambiguous',
			reason: 'ambiguous-multi-source-owner',
			revision: null,
			source_revision: null,
			observed_at: '2026-10-22T00:00:00Z'
		}
	]
};

function deferred() {
	let resolve;
	let reject;
	const promise = new Promise((resolvePromise, rejectPromise) => {
		resolve = resolvePromise;
		reject = rejectPromise;
	});
	return { promise, resolve, reject };
}

describe('AutomationPage', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.getGitOpsStatus.mockResolvedValue(gitOpsStatus);
		api.getGitOpsCoverage.mockResolvedValue(coverage);
		api.getGitOpsBindings.mockResolvedValue({ items: [] });
		api.refreshGitOpsDiscovery.mockResolvedValue({ cluster: 'cluster-with-private-data' });
		api.resolveGitOpsBinding.mockResolvedValue({ status: 'resolved' });
		api.getAutomationStatus.mockResolvedValue({enabled: true, provider: 'gitops'});
		api.getOpenPullRequests.mockResolvedValue([
			{
				id: 1,
				recommendation_id: 101,
				plan_id: 'plan-101',
				provider: 'github',
				provider_id: 'production-github',
				repo: 'acme/platform-config',
				status: 'open',
				pr_url: 'https://user:pr-secret@example.test/acme/platform-config/pull/1',
				error: 'raw error with hidden-secret',
				sync_status: 'pending',
				sync_revision: 'a'.repeat(40),
				sync_observed_at: '2026-10-21T23:00:00Z',
				sync_error_code: 'reconciliation_pending'
			},
			{
				id: 2,
				recommendation_id: 102,
				plan_id: 'plan-102',
				provider: 'gitlab',
				provider_id: 'production-gitlab',
				repo: 'acme/platform-config',
				status: 'pending',
				sync_status: 'failed',
				sync_revision: null,
				sync_observed_at: null,
				sync_error_code: 'controller_failed'
			},
			{
				id: 3,
				recommendation_id: 103,
				plan_id: 'Bearer plan-secret',
				provider: 'https://user:provider-secret@example.test',
				provider_id: 'ssh://provider-secret',
				repo: 'https://user:repo-secret@example.test/private',
				status: 'open',
				sync_status: 'Bearer sync-secret',
				sync_revision: 'Bearer revision-secret',
				sync_error_code: 'raw-error-secret'
			}
		]);
	});

	it('shows safe discovery, coverage, provider readiness, and pending/open PR metadata', async () => {
		const { container } = render(AutomationPage);

		expect(await screen.findByRole('heading', { name: 'GitOps discovery' })).toBeInTheDocument();
		expect(screen.getByText('Argo CD')).toBeInTheDocument();
		expect(screen.getByText('Flux Kustomization')).toBeInTheDocument();
		expect(screen.getByText('CRD not detected')).toBeInTheDocument();
		expect(screen.getByText('80%')).toBeInTheDocument();
		expect(
			screen.getByText(/Controller availability, workload bindings, and current discovery coverage/i)
		).toBeInTheDocument();

		expect(screen.getByRole('heading', { name: 'GitOps plan automation readiness' })).toBeInTheDocument();
		expect(
			screen.getByText(/provider Secrets are mounted read-only; secret values are never displayed/i)
		).toBeInTheDocument();
		expect(screen.getByText('GitOps plan automation is ready')).toBeInTheDocument();
		expect(screen.getByText('Provider registry: Configured')).toBeInTheDocument();

		expect(screen.getByText('plan-101')).toBeInTheDocument();
		expect(screen.getByText('production-github')).toBeInTheDocument();
		expect(screen.getAllByText('acme/platform-config')).toHaveLength(2);
		expect(screen.getByText('a'.repeat(40))).toBeInTheDocument();
		expect(screen.getByText('Reconciliation pending')).toBeInTheDocument();
		expect(screen.getByText('Controller failed')).toBeInTheDocument();
		expect(screen.getAllByText('Pending').length).toBeGreaterThan(0);
		expect(screen.getAllByText('Open')).toHaveLength(2);

		expect(container).not.toHaveTextContent('cluster-with-private-data');
		expect(container).not.toHaveTextContent('legacy-secret');
		expect(container).not.toHaveTextContent('pr-secret');
		expect(container).not.toHaveTextContent('hidden-secret');
		expect(container).not.toHaveTextContent('plan-secret');
		expect(container).not.toHaveTextContent('provider-secret');
		expect(container).not.toHaveTextContent('repo-secret');
		expect(container).not.toHaveTextContent('sync-secret');
		expect(container).not.toHaveTextContent('revision-secret');
		expect(container).not.toHaveTextContent('raw-error-secret');
		expect(container).not.toHaveTextContent('unsafe provider detail');
	});

	it('shows a loading state while the read-only status requests are pending', () => {
		api.getGitOpsStatus.mockReturnValue(deferred().promise);
		api.getGitOpsCoverage.mockReturnValue(deferred().promise);
		api.getGitOpsBindings.mockReturnValue(deferred().promise);
		api.getAutomationStatus.mockReturnValue(deferred().promise);
		api.getOpenPullRequests.mockReturnValue(deferred().promise);

		render(AutomationPage);

		expect(screen.getByText('Loading…')).toBeInTheDocument();
	});

	it('shows a generic error without rendering server error details', async () => {
		const error = new Error('Authorization: Bearer server-secret');
		api.getGitOpsStatus.mockRejectedValue(error);
		api.getGitOpsCoverage.mockRejectedValue(error);
		api.getGitOpsBindings.mockRejectedValue(error);
		api.getAutomationStatus.mockRejectedValue(error);
		api.getOpenPullRequests.mockRejectedValue(error);

		const { container } = render(AutomationPage);

		expect(await screen.findByText('Automation operations could not be loaded.')).toBeInTheDocument();
		expect(container).not.toHaveTextContent('server-secret');
	});

	it('shows an empty state when no pending or open pull requests are returned', async () => {
		api.getOpenPullRequests.mockResolvedValue([]);

		render(AutomationPage);

		expect(await screen.findByText('No pending or open pull requests.')).toBeInTheDocument();
	});

	it('requires explicit confirmation, resolves only a listed candidate, and refreshes coverage', async () => {
		api.getGitOpsBindings.mockResolvedValue({ items: [actionableBinding] });
		const { container } = render(AutomationPage);

		expect(await screen.findByRole('heading', { name: 'Workload binding issues and manual resolution' })).toBeInTheDocument();
		expect(screen.getByText('Multiple current sources disagree')).toBeInTheDocument();
		expect(screen.getAllByText(/github\.example\.test\/acme\/platform-config\.git/).length).toBeGreaterThan(0);
		expect(screen.getAllByText(/Multiple source owners were found/).length).toBeGreaterThan(0);
		expect(screen.getAllByText(/github\.example\.test\/acme\/other\.git/).length).toBeGreaterThan(0);
		expect(container).not.toHaveTextContent('private-cluster-name');
		expect(container).not.toHaveTextContent('credential');
		expect(container).not.toHaveTextContent('token=secret');

		const candidateSelect = screen.getByLabelText('Candidate source for payments/Deployment/api');
		await fireEvent.change(candidateSelect, {
			target: { value: actionableBinding.candidates[0].id }
		});
		const resolveButton = screen.getByRole('button', { name: 'Resolve binding' });
		expect(resolveButton).toBeDisabled();
		await fireEvent.click(
			screen.getByLabelText('I confirm this source is the correct GitOps owner')
		);
		expect(resolveButton).toBeEnabled();
		await fireEvent.click(resolveButton);

		await waitFor(() => {
			expect(api.resolveGitOpsBinding).toHaveBeenCalledWith({
				namespace: 'payments',
				workload_kind: 'Deployment',
				workload_name: 'api',
				candidate_id: actionableBinding.candidates[0].id,
				generation_token: actionableBinding.candidates[0].generation_token,
				confirmed: true
			});
			expect(api.getGitOpsCoverage).toHaveBeenCalledTimes(2);
			expect(api.getGitOpsBindings).toHaveBeenCalledTimes(2);
		});
		expect(await screen.findByText('Manual binding resolution saved.')).toBeInTheDocument();
	});

	it('shows blocked and stale bindings with provider-unresolved reasons without offering unsafe resolution', async () => {
		const blockedBinding = {
			...actionableBinding,
			workload_name: 'web',
			status: 'blocked',
			reason: 'provider-unresolved',
			candidates: []
		};
		const staleBinding = {
			...actionableBinding,
			namespace: 'data',
			workload_kind: 'StatefulSet',
			workload_name: 'database',
			status: 'stale',
			reason: 'source-not-observed-in-refresh',
			candidates: []
		};
		api.getGitOpsBindings.mockResolvedValue({ items: [blockedBinding, staleBinding] });

		render(AutomationPage);

		expect(await screen.findByText('No configured provider matches this source')).toBeInTheDocument();
		expect(screen.getAllByText('Blocked').length).toBeGreaterThan(1);
		expect(screen.getAllByText('Stale').length).toBeGreaterThan(1);
		expect(
			screen.getByText('The source was not observed in the latest discovery refresh')
		).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Resolve binding' })).not.toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Refresh discovery & operations' })).toBeInTheDocument();
	});

	it('triggers a discovery refresh and reports refresh failures visibly', async () => {
		api.refreshGitOpsDiscovery.mockRejectedValue(new Error('Kubernetes discovery is unavailable.'));
		render(AutomationPage);
		const refreshButton = await screen.findByRole('button', { name: 'Refresh discovery & operations' });
		await waitFor(() => expect(refreshButton).toBeEnabled());

		await fireEvent.click(refreshButton);

		expect(await screen.findByText('Kubernetes discovery is unavailable.')).toBeInTheDocument();
		expect(api.refreshGitOpsDiscovery).toHaveBeenCalledTimes(1);
	});

	it('clears selected candidates and confirmations when the candidate generation changes', async () => {
		const refreshedBinding = {
			...actionableBinding,
			candidates: [
				{
					...actionableBinding.candidates[0],
					generation_token: `sha256:${'c'.repeat(64)}`
				}
			]
		};
		api.getGitOpsBindings
			.mockResolvedValueOnce({ items: [actionableBinding] })
			.mockResolvedValueOnce({ items: [refreshedBinding] });

		render(AutomationPage);
		const candidateSelect = await screen.findByLabelText('Candidate source for payments/Deployment/api');
		await fireEvent.change(candidateSelect, {
			target: { value: actionableBinding.candidates[0].id }
		});
		await fireEvent.click(screen.getByLabelText('I confirm this source is the correct GitOps owner'));
		expect(screen.getByRole('button', { name: 'Resolve binding' })).toBeEnabled();

		await fireEvent.click(screen.getByRole('button', { name: 'Refresh discovery & operations' }));

		await waitFor(() => {
			expect(api.getGitOpsBindings).toHaveBeenCalledTimes(2);
			expect(screen.getByLabelText('Candidate source for payments/Deployment/api').value).toBe('');
			expect(
				screen.getByLabelText('I confirm this source is the correct GitOps owner').checked
			).toBe(false);
		});
		expect(screen.getByRole('button', { name: 'Resolve binding' })).toBeDisabled();
		expect(
			screen
				.getByLabelText('Candidate source for payments/Deployment/api')
				.querySelector(`option[value="${actionableBinding.candidates[0].id}"]`)
		).toBeInTheDocument();
	});

	it('clears a confirmation when the available candidate set changes', async () => {
		const refreshedBinding = {
			...actionableBinding,
			candidates: [actionableBinding.candidates[0]]
		};
		api.getGitOpsBindings
			.mockResolvedValueOnce({ items: [actionableBinding] })
			.mockResolvedValueOnce({ items: [refreshedBinding] });

		render(AutomationPage);
		await fireEvent.change(
			await screen.findByLabelText('Candidate source for payments/Deployment/api'),
			{ target: { value: actionableBinding.candidates[0].id } }
		);
		await fireEvent.click(screen.getByLabelText('I confirm this source is the correct GitOps owner'));
		expect(screen.getByRole('button', { name: 'Resolve binding' })).toBeEnabled();

		await fireEvent.click(screen.getByRole('button', { name: 'Refresh discovery & operations' }));

		await waitFor(() => {
			expect(screen.getByLabelText('Candidate source for payments/Deployment/api').value).toBe('');
			expect(
				screen.getByLabelText('I confirm this source is the correct GitOps owner').checked
			).toBe(false);
		});
		expect(screen.getByRole('button', { name: 'Resolve binding' })).toBeDisabled();
	});

	it('shows a generic stale-candidate error without exposing server details', async () => {
		api.getGitOpsBindings.mockResolvedValue({ items: [actionableBinding] });
		api.resolveGitOpsBinding.mockRejectedValue(new Error('provider credential: hidden-secret'));
		const { container } = render(AutomationPage);

		await screen.findByRole('heading', { name: 'Workload binding issues and manual resolution' });
		await fireEvent.change(
			screen.getByLabelText('Candidate source for payments/Deployment/api'),
			{ target: { value: actionableBinding.candidates[0].id } }
		);
		await fireEvent.click(
			screen.getByLabelText('I confirm this source is the correct GitOps owner')
		);
		await fireEvent.click(screen.getByRole('button', { name: 'Resolve binding' }));

		expect(await screen.findByRole('alert')).toHaveTextContent(
			'Binding resolution failed. Refresh candidates and try again.'
		);
		expect(container).not.toHaveTextContent('hidden-secret');
	});

	it('refreshes the read-only operations sources on demand', async () => {
		render(AutomationPage);
		await screen.findByRole('heading', { name: 'GitOps discovery' });

		await fireEvent.click(screen.getByRole('button', { name: 'Refresh discovery & operations' }));

		await waitFor(() => {
			expect(api.getGitOpsStatus).toHaveBeenCalledTimes(2);
			expect(api.getGitOpsCoverage).toHaveBeenCalledTimes(2);
			expect(api.getGitOpsBindings).toHaveBeenCalledTimes(2);
			expect(api.getAutomationStatus).toHaveBeenCalledTimes(2);
			expect(api.getOpenPullRequests).toHaveBeenCalledTimes(2);
		});
	});
});
