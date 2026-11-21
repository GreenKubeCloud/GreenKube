/**
 * Tests for GitOps discovery, manual binding resolution, and automation operations.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import AutomationPage from '../../src/routes/automation/+page.svelte';
import * as api from '#lib/api.js';
import { currentUser } from '#lib/stores.js';

vi.mock('#lib/api.js', () => ({
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

	it('shows safe discovery, coverage and provider readiness without a pull-request list', async () => {
		const { container } = render(AutomationPage);

		expect(await screen.findByRole('heading', { name: 'GitOps discovery' })).toBeInTheDocument();
		expect(screen.getByRole('heading', { level: 3, name: 'Argo CD' })).toBeInTheDocument();
		expect(screen.getByRole('heading', { level: 3, name: 'Flux' })).toBeInTheDocument();
		expect(screen.getByText('Kustomizations')).toBeInTheDocument();
		expect(screen.getByText(/CRD not detected/)).toBeInTheDocument();
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

		// Pull requests moved to the changes board (K4): no second list here.
		expect(api.getOpenPullRequests).not.toHaveBeenCalled();
		expect(screen.queryByText('plan-101')).not.toBeInTheDocument();

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

	it('renders exactly one card per GitOps controller family with grouped Flux resource kinds', async () => {
		render(AutomationPage);

		const controllers = await screen.findByRole('list', { name: 'GitOps controllers' });
		const cards = within(controllers).getAllByRole('article');
		expect(cards).toHaveLength(2);
		expect(cards.map((card) => card.getAttribute('data-family'))).toEqual(['argocd', 'flux']);

		const argo = within(controllers).getByRole('article', { name: 'Argo CD' });
		expect(within(argo).getByLabelText('Argo CD status: Detected')).toBeInTheDocument();

		const flux = within(controllers).getByRole('article', { name: 'Flux' });
		expect(within(flux).getByLabelText('Flux status: Degraded')).toBeInTheDocument();
		expect(within(flux).getByText(/2 of 6 resource kinds available/)).toBeInTheDocument();
		expect(within(flux).getAllByRole('listitem')).toHaveLength(6);
		expect(within(flux).getByRole('list', { name: 'Reconcilers' })).toHaveTextContent('HelmReleases');
		expect(within(flux).getByRole('list', { name: 'Sources' })).toHaveTextContent('GitRepositories');
		expect(screen.queryByText('Flux Kustomization')).not.toBeInTheDocument();
	});

	it('summarises workload coverage in one panel instead of one card per status', async () => {
		render(AutomationPage);

		const coverageSection = await screen.findByRole('region', { name: 'Workload coverage' });
		expect(within(coverageSection).getByRole('progressbar', { name: 'Workload coverage' })).toHaveAttribute(
			'aria-valuenow',
			'80'
		);
		const breakdown = within(coverageSection).getByRole('list', { name: 'Workload bindings by status' });
		expect(within(breakdown).getAllByRole('listitem')).toHaveLength(6);
		expect(within(breakdown).getByText('Resolved').closest('li')).toHaveTextContent('8');
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

	it('links to the changes board instead of listing pull requests (K4)', async () => {
		render(AutomationPage);

		const section = await screen.findByTestId('changes-board-link');
		expect(within(section).getByRole('heading', { level: 2, name: 'Pull requests and changes' })).toBeInTheDocument();
		expect(within(section).getByRole('link', { name: /Open the changes board/ })).toHaveAttribute('href', '/changes');
		expect(screen.queryByText('No pending or open pull requests.')).not.toBeInTheDocument();
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
		});
		expect(api.getOpenPullRequests).not.toHaveBeenCalled();
	});

	it('gives a viewer a read-only reload and no binding resolution (no 403 actions)', async () => {
		currentUser.set({ kind: 'user', name: 'vera', role: 'viewer', scopes: ['read'] });
		api.getGitOpsBindings.mockResolvedValue({ items: [actionableBinding] });
		render(AutomationPage);

		expect(await screen.findByTestId('read-only-hint')).toHaveTextContent('Read-only access (viewer)');
		expect(await screen.findByTestId('resolve-read-only')).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Resolve binding' })).not.toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Refresh discovery & operations' })).not.toBeInTheDocument();

		const reload = screen.getByRole('button', { name: 'Reload' });
		await waitFor(() => expect(reload).toBeEnabled());
		await fireEvent.click(reload);

		await waitFor(() => expect(api.getGitOpsStatus).toHaveBeenCalledTimes(2));
		expect(api.refreshGitOpsDiscovery).not.toHaveBeenCalled();
	});
});
