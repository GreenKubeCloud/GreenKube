/**
 * Setup checklist (F1, K6), repository suggestions (F2) and the Sources view
 * (E5, K6). The setup tests answer with JSON generated from the FastAPI app
 * (tests/fixtures/api, written by tests/api/test_ui_contract_fixtures.py).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import SetupPage from '../../src/routes/setup/+page.svelte';
import SourcesPage from '../../src/routes/sources/+page.svelte';
import * as api from '#lib/api.js';
import { currentUser } from '#lib/stores.js';
import { setupStatus } from '#lib/setup.js';
import INCOMPLETE_FIXTURE from '../fixtures/api/setup_status_incomplete.json';
import HOST_MISMATCH_FIXTURE from '../fixtures/api/setup_status_host_mismatch.json';
import PAUSED_FIXTURE from '../fixtures/api/setup_status_paused.json';
import COMPLETE_FIXTURE from '../fixtures/api/setup_status_complete.json';
import SUGGESTIONS_FIXTURE from '../fixtures/api/repository_suggestions.json';
import ALLOWLIST_FIXTURE from '../fixtures/api/repository_allowlist.json';

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
		getSetupStatus: vi.fn(),
		getRepositorySuggestions: vi.fn(),
		allowlistRepositories: vi.fn(),
		listSources: vi.fn(),
		listSourceRuns: vi.fn(),
		getSourceTrackRecord: vi.fn()
	};
});

// Responses generated from the FastAPI app (tests/api/test_ui_contract_fixtures.py).
const STATUS = INCOMPLETE_FIXTURE;
const SUGGESTIONS = SUGGESTIONS_FIXTURE;
const OTHER_REPO = 'https://gitea.example.com/platform/apps.git';

const ADMIN = { scopes: ['admin', 'read', 'write'] };
const READER = { scopes: ['read'] };

describe('/setup', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		currentUser.set(ADMIN);
		setupStatus.set(null);
		api.getSetupStatus.mockResolvedValue(STATUS);
		api.getRepositorySuggestions.mockResolvedValue(SUGGESTIONS);
		api.allowlistRepositories.mockResolvedValue(ALLOWLIST_FIXTURE);
	});

	it('groups the server checks by category with status words, fixes and safe documentation links', async () => {
		render(SetupPage);

		// recommendation_sources, repositories, git_credentials and renderer need attention.
		expect(await screen.findByTestId('setup-summary')).toHaveTextContent('4 checks need attention');
		expect(within(screen.getByTestId('check-group-data_sources')).getAllByText('OK')).toHaveLength(1);
		const credentials = screen.getByTestId('check-git_credentials');
		expect(credentials).toHaveTextContent('Action needed');
		expect(credentials).toHaveTextContent('not bound to a host');
		expect(credentials).toHaveTextContent('Fix: Add the host key to the Secret');
		expect(credentials).toHaveTextContent('kubectl -n greenkube patch secret greenkube-git-credentials');
		expect(within(credentials).getByRole('link', { name: /Documentation/ })).toHaveAttribute(
			'href',
			'https://github.com/GreenKubeCloud/GreenKube/blob/main/docs/installation.md#optional-connect-git'
		);
		// A passing check shows no fix.
		const prometheus = screen.getByTestId('check-prometheus');
		expect(prometheus).not.toHaveTextContent('Fix:');
		expect(screen.getByTestId('check-opencost')).toHaveTextContent('Not applicable');
		expect(screen.getByTestId('check-renderer')).toHaveTextContent('renderer_egress_unenforced');
		// The layout banner reads the refreshed status.
		expect(get(setupStatus)?.complete).toBe(false);
	});

	it('routes every ui_action the server emits to an in-app link', async () => {
		render(SetupPage);

		const repositories = await screen.findByTestId('check-repositories');
		expect(within(repositories).getByRole('link', { name: /Review repository suggestions/ })).toHaveAttribute(
			'href',
			'/setup#repository-suggestions'
		);
		const sources = screen.getByTestId('check-recommendation_sources');
		expect(within(sources).getByRole('link', { name: /Open recommendation sources/ })).toHaveAttribute('href', '/sources');
		expect(sources).not.toHaveTextContent('open_sources');
	});

	it('shows the capability states and readable reasons from the contract', async () => {
		render(SetupPage);

		expect(await screen.findByTestId('capability-discovery')).toHaveTextContent('active');
		const delivery = screen.getByTestId('capability-delivery');
		expect(delivery).toHaveTextContent('dormant');
		expect(delivery).toHaveTextContent('The Git token is not bound to a host');
		expect(screen.queryByTestId('delivery-paused')).not.toBeInTheDocument();
	});

	it('explains a token bound to another host', async () => {
		api.getSetupStatus.mockResolvedValue(HOST_MISMATCH_FIXTURE);
		render(SetupPage);

		const credentials = await screen.findByTestId('check-git_credentials');
		expect(credentials).toHaveTextContent('none of the discovered repositories use');
		expect(screen.getByTestId('capability-delivery')).toHaveTextContent("The Git token's host matches no discovered repository.");
	});

	it('shows delivery paused by policy, not as a failure of the controller', async () => {
		api.getSetupStatus.mockResolvedValue(PAUSED_FIXTURE);
		render(SetupPage);

		expect(await screen.findByTestId('delivery-paused')).toHaveTextContent('automation.policy.pauseDelivery');
		expect(screen.getByTestId('capability-delivery')).toHaveTextContent('paused');
		expect(screen.getByTestId('check-git_credentials')).toHaveTextContent('Blocked');
		expect(screen.getByTestId('check-git_credentials')).toHaveTextContent('pauseDelivery=false');
	});

	it('copies a fix snippet', async () => {
		const writeText = vi.fn(() => Promise.resolve());
		Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
		render(SetupPage);

		await fireEvent.click(await screen.findByRole('button', { name: 'Copy the fix for Git credentials' }));

		expect(writeText).toHaveBeenCalledWith(expect.stringContaining('greenkube-git-credentials'));
		expect(await screen.findByText('Copied')).toBeInTheDocument();
	});

	it('allows the selected discovered repositories and shows the values snippet the server returns (F2)', async () => {
		render(SetupPage);

		const button = await screen.findByTestId('allow-repositories');
		// The suggestions list one repository to allow and one already allowlisted.
		expect(button).toHaveTextContent('Allow this repository');

		await fireEvent.click(button);

		await waitFor(() => expect(api.allowlistRepositories).toHaveBeenCalledWith([OTHER_REPO]));
		expect(await screen.findByText('1 repository was added to the allowlist.')).toBeInTheDocument();
		// values_snippet: the automation.repositoryAllowlist values a GitOps-managed install commits.
		expect(screen.getByTestId('allowlist-snippet')).toHaveTextContent('repositoryAllowlist:');
		expect(screen.getByTestId('allowlist-snippet')).toHaveTextContent('- platform/apps');
		expect(api.getSetupStatus).toHaveBeenCalledTimes(2);
		expect(screen.getByText(/Already allowlisted \(1\)/)).toBeInTheDocument();
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
		api.getSetupStatus.mockResolvedValue(COMPLETE_FIXTURE);
		api.getRepositorySuggestions.mockResolvedValue({ suggestions: [] });
		render(SetupPage);

		expect(await screen.findByTestId('setup-summary')).toHaveTextContent('Setup is complete');
		expect(screen.getByTestId('capability-delivery')).toHaveTextContent('active');
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
