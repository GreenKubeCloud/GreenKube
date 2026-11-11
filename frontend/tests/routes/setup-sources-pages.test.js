/**
 * Setup checklist (F1, K6), repository suggestions (F2) and the Sources view
 * (E5, K6), built against the wave 3b API contracts (mocked here).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import SetupPage from '../../src/routes/setup/+page.svelte';
import SourcesPage from '../../src/routes/sources/+page.svelte';
import * as api from '$lib/api.js';
import { currentUser } from '$lib/stores.js';
import { setupStatus } from '$lib/setup.js';

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
		getSetupStatus: vi.fn(),
		getRepositorySuggestions: vi.fn(),
		allowlistRepositories: vi.fn(),
		listSources: vi.fn(),
		listSourceRuns: vi.fn(),
		getSourceTrackRecord: vi.fn()
	};
});

const STATUS = {
	complete: false,
	checks: [
		{ id: 'prometheus', category: 'data_sources', title: 'Prometheus', status: 'ok', reason: 'Found at prometheus:9090.', fix: { text: 'Nothing to do.' }, docs_url: 'https://greenkube.cloud/docs/prometheus' },
		{
			id: 'git-credentials',
			category: 'credentials',
			title: 'Git credentials for github.com',
			status: 'action_needed',
			reason: 'No write token for github.com.',
			fix: {
				text: 'Create the credentials Secret.',
				snippet: 'kubectl create secret generic greenkube-git-credentials --from-literal=token=...',
				ui_action: 'allowlist_repositories'
			},
			docs_url: 'javascript:alert(1)'
		},
		{ id: 'evidence', category: 'evidence', title: 'Evidence warm-up', status: 'pending', reason: '31h of history needed.', fix: null, docs_url: '' }
	],
	capabilities: {
		discovery: { state: 'active', reason: 'Argo CD found.' },
		delivery: { state: 'dormant', reason: 'No write credentials.' },
		sync_tracking: { state: 'active', reason: '' },
		measurement: { state: 'active', reason: '' }
	}
};

const SUGGESTIONS = {
	suggestions: [
		{ repo_url: 'https://github.com/acme/deploy', provider_type: 'github', api_base_url: 'https://api.github.com', inferred_from: 'argocd/shop', allowlisted: false },
		{ repo_url: 'https://gitlab.com/acme/infra', provider_type: 'gitlab', api_base_url: 'https://gitlab.com/api/v4', inferred_from: 'flux/infra', allowlisted: false },
		{ repo_url: 'https://github.com/acme/old', provider_type: 'github', api_base_url: '', inferred_from: 'argocd/old', allowlisted: true }
	]
};

const ADMIN = { scopes: ['admin', 'read', 'write'] };
const READER = { scopes: ['read'] };

describe('/setup', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		currentUser.set(ADMIN);
		setupStatus.set(null);
		api.getSetupStatus.mockResolvedValue(STATUS);
		api.getRepositorySuggestions.mockResolvedValue(SUGGESTIONS);
		api.allowlistRepositories.mockResolvedValue({ allowlisted: ['https://github.com/acme/deploy'] });
	});

	it('groups the checks by category with status words, fixes and safe documentation links', async () => {
		render(SetupPage);

		expect(await screen.findByTestId('setup-summary')).toHaveTextContent('1 check needs attention');
		expect(within(screen.getByTestId('check-group-data_sources')).getByText('OK')).toBeInTheDocument();
		const credentials = screen.getByTestId('check-git-credentials');
		expect(credentials).toHaveTextContent('Action needed');
		expect(credentials).toHaveTextContent('Fix: Create the credentials Secret.');
		expect(credentials).toHaveTextContent('kubectl create secret generic');
		expect(within(credentials).getByRole('link', { name: /Review repository suggestions/ })).toHaveAttribute(
			'href',
			'/setup#repository-suggestions'
		);
		// A non-https documentation URL is never linked.
		expect(within(credentials).queryByRole('link', { name: /Documentation/ })).not.toBeInTheDocument();
		// A passing check shows no fix.
		const prometheus = screen.getByTestId('check-prometheus');
		expect(prometheus).not.toHaveTextContent('Nothing to do.');
		expect(within(prometheus).getByRole('link', { name: /Documentation/ })).toHaveAttribute('href', 'https://greenkube.cloud/docs/prometheus');
		expect(screen.getByTestId('check-evidence')).toHaveTextContent('Pending');
		// The layout banner reads the refreshed status.
		expect(get(setupStatus)?.complete).toBe(false);
	});

	it('shows the capability states from the contract', async () => {
		render(SetupPage);

		expect(await screen.findByTestId('capability-discovery')).toHaveTextContent('active');
		expect(screen.getByTestId('capability-delivery')).toHaveTextContent('dormant');
		expect(screen.getByTestId('capability-delivery')).toHaveTextContent('No write credentials.');
	});

	it('copies a fix snippet', async () => {
		const writeText = vi.fn(() => Promise.resolve());
		Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
		render(SetupPage);

		await fireEvent.click(await screen.findByRole('button', { name: 'Copy the fix for Git credentials for github.com' }));

		expect(writeText).toHaveBeenCalledWith(expect.stringContaining('greenkube-git-credentials'));
		expect(await screen.findByText('Copied')).toBeInTheDocument();
	});

	it('allows the selected discovered repositories (F2)', async () => {
		render(SetupPage);

		const button = await screen.findByTestId('allow-repositories');
		expect(button).toHaveTextContent('Allow these 2 repositories');
		await fireEvent.click(screen.getByRole('checkbox', { name: /gitlab\.com\/acme\/infra/ }));
		expect(button).toHaveTextContent('Allow this repository');

		await fireEvent.click(button);

		await waitFor(() => expect(api.allowlistRepositories).toHaveBeenCalledWith(['https://github.com/acme/deploy']));
		expect(await screen.findByText('1 repository was added to the allowlist.')).toBeInTheDocument();
		expect(api.getSetupStatus).toHaveBeenCalledTimes(2);
		expect(screen.getByText(/Already allowlisted \(1\)/)).toBeInTheDocument();
	});

	it('shows the values snippet a GitOps-managed install must commit', async () => {
		api.allowlistRepositories.mockResolvedValue({
			message: 'Commit this snippet to your Helm values.',
			snippet: 'automation:\n  providers:\n    - allowlist: [acme/deploy]'
		});
		render(SetupPage);

		await fireEvent.click(await screen.findByTestId('allow-repositories'));

		expect(await screen.findByTestId('allowlist-snippet')).toHaveTextContent('allowlist: [acme/deploy]');
		expect(screen.getByText('Commit this snippet to your Helm values.')).toBeInTheDocument();
	});

	it('explains a refused allowlist change', async () => {
		api.allowlistRepositories.mockRejectedValue(new api.ApiError('Forbidden', 403));
		render(SetupPage);

		await fireEvent.click(await screen.findByTestId('allow-repositories'));

		expect(await screen.findByRole('alert')).toHaveTextContent('Only an administrator can change the repository allowlist.');
	});

	it('lets only administrators allow repositories', async () => {
		currentUser.set(READER);
		render(SetupPage);

		expect(await screen.findByText('An administrator must allow these repositories.')).toBeInTheDocument();
		expect(screen.queryByTestId('allow-repositories')).not.toBeInTheDocument();
		for (const box of screen.getAllByRole('checkbox')) expect(box).toBeDisabled();
	});

	it('reports a complete setup', async () => {
		api.getSetupStatus.mockResolvedValue({
			...STATUS,
			complete: true,
			checks: STATUS.checks.map((check) => ({ ...check, status: 'ok' }))
		});
		api.getRepositorySuggestions.mockResolvedValue({ suggestions: [] });
		render(SetupPage);

		expect(await screen.findByTestId('setup-summary')).toHaveTextContent('Setup is complete');
		expect(screen.getByTestId('suggestions-empty')).toBeInTheDocument();
	});

	it('degrades gracefully on a server without the setup endpoints', async () => {
		api.getSetupStatus.mockRejectedValue(new api.ApiError('Not Found', 404));
		api.getRepositorySuggestions.mockRejectedValue(new api.ApiError('Not Found', 404));
		render(SetupPage);

		expect(await screen.findByTestId('setup-unsupported')).toBeInTheDocument();
		expect(screen.getByTestId('repository-suggestions')).toHaveTextContent('does not suggest repositories yet');
	});
});

const SOURCES = {
	sources: [
		{
			id: 'greenkube.rightsizing',
			display_name: 'GreenKube rightsizing',
			description: 'Native analyzer.',
			tier: 'native',
			origin: 'builtin',
			capabilities: ['container_requests'],
			version: '1.0',
			legacy_source: 'greenkube',
			enabled: 'auto',
			status: 'healthy',
			last_run: { finished_at: new Date(Date.now() - 60_000).toISOString(), status: 'succeeded' },
			last_success_at: new Date(Date.now() - 60_000).toISOString(),
			signal_count: 12,
			recommendation_count: 4
		},
		{
			id: 'vpa',
			display_name: 'Vertical Pod Autoscaler',
			tier: 'oss',
			origin: 'builtin',
			status: 'failing',
			status_reason: 'enabled explicitly but unavailable: VPA CRD not found',
			signal_count: 0,
			recommendation_count: 0
		}
	],
	problems: [{ code: 'plugin_load_failed', message: 'Could not import acme_source.', source_id: 'acme', entry_point: 'acme' }]
};

describe('/sources', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.listSources.mockResolvedValue(SOURCES);
		api.getSourceTrackRecord.mockResolvedValue({
			entries: [{ source_family: 'greenkube', capability: 'container_requests', summary: '14/16 realized', decided: 16 }]
		});
		api.listSourceRuns.mockResolvedValue([
			{
				run_id: 7,
				status: 'incomplete',
				finished_at: new Date().toISOString(),
				duration_seconds: 1.25,
				signal_count: 3,
				error_code: 'partial',
				diagnostics: [{ level: 'warning', code: 'skipped', message: '12 VPAs skipped: updateMode=Auto' }]
			}
		]);
	});

	it('lists sources with problems first, their status in words and the track record', async () => {
		render(SourcesPage);

		const list = await screen.findByTestId('sources-list');
		const items = within(list).getAllByRole('heading', { level: 2 }).map((node) => node.textContent);
		expect(items[0]).toContain('Vertical Pod Autoscaler');
		expect(screen.getByTestId('source-vpa')).toHaveTextContent('Failing');
		expect(screen.getByTestId('source-vpa')).toHaveTextContent('VPA CRD not found');
		expect(screen.getByTestId('source-greenkube.rightsizing')).toHaveTextContent('Healthy');
		expect(screen.getByTestId('track-record')).toHaveTextContent('container_requests: 14/16 realized');
		expect(screen.getByTestId('source-problems')).toHaveTextContent('Could not import acme_source.');
	});

	it('expands the recent runs of a source on demand', async () => {
		render(SourcesPage);

		const source = await screen.findByTestId('source-greenkube.rightsizing');
		const toggle = within(source).getByRole('button', { name: 'Show recent runs' });
		expect(toggle).toHaveAttribute('aria-expanded', 'false');
		await fireEvent.click(toggle);

		const runs = await within(source).findByTestId('runs-greenkube.rightsizing');
		expect(api.listSourceRuns).toHaveBeenCalledWith('greenkube.rightsizing', expect.objectContaining({ limit: 20 }));
		expect(runs).toHaveTextContent('incomplete');
		expect(runs).toHaveTextContent('1.3 s');
		expect(runs).toHaveTextContent('warning: 12 VPAs skipped: updateMode=Auto');
		expect(within(source).getByRole('button', { name: 'Hide runs' })).toHaveAttribute('aria-expanded', 'true');
	});

	it('reports an unavailable registry and an empty one', async () => {
		api.listSources.mockRejectedValue(new Error('Source run history is unavailable.'));
		const { unmount } = render(SourcesPage);
		expect(await screen.findByRole('alert')).toHaveTextContent('Source run history is unavailable.');
		unmount();

		api.listSources.mockResolvedValue({ sources: [], problems: [] });
		render(SourcesPage);
		expect(await screen.findByTestId('sources-empty')).toBeInTheDocument();
	});
});
