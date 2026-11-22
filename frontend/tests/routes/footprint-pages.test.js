/**
 * Footprint (K7): the dashboard charts, nodes and the report builder live
 * under /footprint; /report and /nodes redirect there; the main navigation
 * shows Changes and Footprint.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/svelte';
import { isRedirect } from '@sveltejs/kit';

const pageUrl = vi.hoisted(() => ({ href: 'http://localhost:3000/footprint' }));

vi.mock('$app/state', () => ({
	page: {
		get url() {
			return new URL(pageUrl.href);
		},
		params: {},
		route: { id: null },
		status: 200,
		state: {}
	}
}));

vi.mock('#lib/api.js', async (importOriginal) => {
	const actual = await importOriginal();
	return {
		...actual,
		getDashboardSummary: vi.fn(),
		getDashboardTimeseries: vi.fn(),
		getMetricsSummary: vi.fn(),
		getTimeseries: vi.fn(),
		getMetricsByNamespace: vi.fn(),
		getTopPods: vi.fn(),
		refreshDashboardSummary: vi.fn(),
		getNamespaces: vi.fn()
	};
});

vi.mock('echarts', () => ({
	init: () => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() })
}));

import * as api from '#lib/api.js';
import FootprintPage from '../../src/routes/footprint/+page.svelte';
import FootprintLayout from '../../src/routes/footprint/+layout.svelte';
import Layout from '../../src/routes/+layout.svelte';
import { load as reportLoad } from '../../src/routes/report/+page.js';
import { load as nodesLoad } from '../../src/routes/nodes/+page.js';
import { FOOTPRINT_REDIRECTS, isFootprintSectionActive, redirectToFootprint } from '../../src/routes/footprint/sections.js';

function redirectOf(fn) {
	try {
		fn();
	} catch (error) {
		if (isRedirect(error)) return { status: error.status, location: error.location };
		throw error;
	}
	throw new Error('expected a redirect');
}

describe('redirects from the old routes', () => {
	it('sends /report and /nodes to /footprint permanently, keeping the query string', () => {
		expect(redirectOf(() => reportLoad({ url: new URL('http://localhost/report?namespace=shop&last=7d') }))).toEqual({
			status: 308,
			location: '/footprint/report?namespace=shop&last=7d'
		});
		expect(redirectOf(() => nodesLoad({ url: new URL('http://localhost/nodes') }))).toEqual({ status: 308, location: '/footprint/nodes' });
		expect(Object.keys(FOOTPRINT_REDIRECTS).sort()).toEqual(['/nodes', '/report']);
	});

	it('refuses an unknown old route', () => {
		expect(() => redirectToFootprint('/elsewhere', new URL('http://localhost/elsewhere'))).toThrow('No footprint redirect');
	});
});

describe('footprint sections', () => {
	afterEach(() => {
		pageUrl.href = 'http://localhost:3000/footprint';
	});

	it('marks only the current section active', () => {
		expect(isFootprintSectionActive('/footprint', '/footprint')).toBe(true);
		expect(isFootprintSectionActive('/footprint', '/footprint/')).toBe(true);
		expect(isFootprintSectionActive('/footprint', '/footprint/nodes')).toBe(false);
		expect(isFootprintSectionActive('/footprint/nodes', '/footprint/nodes')).toBe(true);
		expect(isFootprintSectionActive('/footprint/report', '/footprint/reports')).toBe(false);
	});

	it('renders a labelled section navigation with the current page', () => {
		pageUrl.href = 'http://localhost:3000/footprint/nodes';
		render(FootprintLayout);

		const nav = screen.getByRole('navigation', { name: 'Footprint sections' });
		const links = within(nav).getAllByRole('link');
		expect(links.map((link) => [link.textContent.trim(), link.getAttribute('href')])).toEqual([
			['Charts', '/footprint'],
			['Nodes', '/footprint/nodes'],
			['Report', '/footprint/report']
		]);
		expect(within(nav).getByRole('link', { name: 'Nodes' })).toHaveAttribute('aria-current', 'page');
		expect(within(nav).getByRole('link', { name: 'Charts' })).not.toHaveAttribute('aria-current');
	});
});

describe('/footprint charts', () => {
	const summary = { total_co2e_grams: 1200, total_embodied_co2e_grams: 0, total_cost: 3, total_energy_joules: 0, pod_count: 4 };
	const point = { bucket_ts: new Date().toISOString(), co2e_grams: 1, embodied_co2e_grams: 0, total_cost: 0, joules: 0 };

	beforeEach(() => {
		vi.clearAllMocks();
		api.getDashboardSummary.mockResolvedValue({ windows: { '24h': summary } });
		api.getDashboardTimeseries.mockResolvedValue({ points: [point] });
		api.getMetricsSummary.mockResolvedValue(summary);
		api.getTimeseries.mockResolvedValue([point]);
		api.getMetricsByNamespace.mockResolvedValue([]);
		api.getTopPods.mockResolvedValue([]);
		api.getNamespaces.mockResolvedValue([]);
	});

	it('shows the footprint tiles and charts moved from the Overview', async () => {
		render(FootprintPage);

		expect(screen.getByRole('heading', { level: 1, name: 'Footprint' })).toBeInTheDocument();
		const charts = await screen.findByTestId('footprint-charts');
		expect(within(charts).getByText('Active Pods')).toBeInTheDocument();
		expect(within(charts).getByText('CO₂ Emissions Over Time')).toBeInTheDocument();
		expect(within(charts).getByText('Top Pods by CO₂')).toBeInTheDocument();
		expect(screen.getByText(/export it as CSV or JSON/)).toBeInTheDocument();
		expect(screen.queryByText(/Grafana/)).not.toBeInTheDocument();
	});

	it('shows the node idle share and the cluster total next to the allocated totals', async () => {
		api.getDashboardSummary.mockResolvedValue({
			windows: {
				'24h': {
					...summary,
					total_co2e_all_scopes: 1200,
					idle_co2e_grams: 600,
					idle_embodied_co2e_grams: 0,
					idle_cost: 1,
					idle_energy_joules: 7200000,
					cluster_total_co2e_grams: 1800,
					cluster_total_embodied_co2e_grams: 0,
					cluster_total_cost: 4,
					cluster_total_energy_joules: 7200000
				}
			}
		});
		render(FootprintPage);

		const table = await screen.findByRole('table', { name: /idle share/i });
		const headers = within(table).getAllByRole('columnheader').map((cell) => cell.textContent.trim());
		expect(headers).toEqual(['', 'Allocated to pods', 'Node idle share', 'Cluster total']);
		const co2e = within(table).getByRole('row', { name: /CO₂e/ });
		expect(within(co2e).getByText('1.20 kg')).toBeInTheDocument();
		expect(within(co2e).getByText(/600/)).toBeInTheDocument();
		expect(within(co2e).getByText('1.80 kg')).toBeInTheDocument();
		const cost = within(table).getByRole('row', { name: /Cost/ });
		expect(within(cost).getByText(/4\.00/)).toBeInTheDocument();
		expect(within(table).queryByText('not available')).not.toBeInTheDocument();
	});

	it('shows "not available" when the idle share is unknown', async () => {
		api.getDashboardSummary.mockResolvedValue({
			windows: {
				'24h': { ...summary, idle_energy_joules: null, cluster_total_energy_joules: null, idle_data_since: new Date(Date.now() - 3600e3).toISOString() }
			}
		});
		render(FootprintPage);

		const table = await screen.findByRole('table', { name: /idle share/i });
		// Idle and cluster total of CO2e, cost and energy.
		expect(within(table).getAllByText('not available')).toHaveLength(6);
		// The idle history starts an hour ago, inside the 24h window.
		expect(screen.getByText(/idle history starts/i)).toBeInTheDocument();
	});

	it('aborts its requests when the page is unmounted', async () => {
		const { unmount } = render(FootprintPage);
		await waitFor(() => expect(api.getDashboardSummary).toHaveBeenCalled());
		const { signal } = api.getDashboardSummary.mock.calls.at(-1)[0];
		unmount();
		expect(signal.aborted).toBe(true);
	});
});

describe('main navigation', () => {
	const originalFetch = globalThis.fetch;

	beforeEach(() => {
		pageUrl.href = 'http://localhost:3000/changes';
		globalThis.fetch = vi.fn((url) => {
			const path = new URL(url).pathname;
			const body =
				path === '/api/v1/auth/me'
					? { authenticated: true, kind: 'user', name: 'admin', role: 'admin', scopes: ['admin', 'read'], must_change_password: false }
					: path === '/api/v1/health'
						? { status: 'ok', version: '0.4.0' }
						: {};
			return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
		});
	});

	afterEach(() => {
		globalThis.fetch = originalFetch;
	});

	it('leads with the loop and groups the visibility pages under Footprint', async () => {
		render(Layout);

		const nav = await screen.findByRole('navigation', { name: 'Main' });
		const labels = within(nav).getAllByRole('link').map((link) => link.getAttribute('aria-label'));
		expect(labels).toEqual(['Overview', 'Recommendations', 'Changes', 'Ledger', 'Footprint', 'Automation', 'Sources', 'Setup', 'Settings']);
		expect(within(nav).getByRole('link', { name: 'Changes' })).toHaveAttribute('aria-current', 'page');
		expect(within(nav).getByRole('link', { name: 'Footprint' })).toHaveAttribute('href', '/footprint');
		expect(within(nav).queryByRole('link', { name: 'Report' })).toBeNull();
		expect(within(nav).queryByRole('link', { name: 'Nodes' })).toBeNull();
	});
});
