/**
 * Demo mode in the layout: GET /health reports `demo: true` only under
 * `greenkube demo`. The footer then says "Demo" instead of the development
 * sign-in warning, and the disconnected services of a cluster-less demo are not
 * shown as errors. A real install keeps both.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import Layout from '../../src/routes/+layout.svelte';
import { clearCsrfToken } from '$lib/api.js';
import { currentUser, demoMode } from '$lib/stores.js';
import { createMemoryStorage, installStorage } from '../helpers/storage.js';

const ANONYMOUS = { authenticated: false, kind: 'anonymous', name: 'anonymous', actor: 'anonymous', scopes: ['read', 'write'] };
const SERVICES = {
	status: 'degraded',
	services: { kubernetes: { name: 'kubernetes', status: 'unreachable', message: 'No kubeconfig', inactive: false } }
};

function ok(body) {
	return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function installServer({ demo }) {
	globalThis.fetch = vi.fn((url) => {
		const path = new URL(url).pathname;
		if (path === '/api/v1/health') return Promise.resolve(ok({ status: 'ok', version: '0.4.0', demo }));
		if (path === '/api/v1/auth/me') return Promise.resolve(ok(ANONYMOUS));
		if (path === '/api/v1/health/services') return Promise.resolve(ok(SERVICES));
		return Promise.resolve(ok({}));
	});
}

let restore = [];

beforeEach(() => {
	restore = [installStorage('sessionStorage', createMemoryStorage()), installStorage('localStorage', createMemoryStorage())];
	clearCsrfToken();
	currentUser.set(null);
	demoMode.set(false);
});

afterEach(() => {
	restore.reverse().forEach((undo) => undo());
	demoMode.set(false);
	vi.restoreAllMocks();
});

describe('demo mode', () => {
	it('shows "Demo" and no disconnected-service dots when the API runs the demo', async () => {
		installServer({ demo: true });
		render(Layout);

		expect(await screen.findByTestId('demo-badge')).toHaveTextContent('Demo');
		expect(get(demoMode)).toBe(true);
		expect(screen.queryByText('Sign-in disabled (development)')).not.toBeInTheDocument();
		expect(screen.queryByText('kubernetes')).not.toBeInTheDocument();
		expect(screen.queryByText('Service Connectivity Issues')).not.toBeInTheDocument();
	});

	it('keeps the development warning and the service health on a real install', async () => {
		installServer({ demo: false });
		render(Layout);

		expect(await screen.findByText('Sign-in disabled (development)')).toBeInTheDocument();
		await waitFor(() => expect(screen.getAllByText('kubernetes').length).toBeGreaterThan(0));
		expect(screen.queryByTestId('demo-badge')).not.toBeInTheDocument();
		expect(get(demoMode)).toBe(false);
	});

	it('shows the neutral tagline, not the retired FinGreenOps claim', async () => {
		installServer({ demo: false });
		render(Layout);

		expect(await screen.findByText('Kubernetes optimization loop')).toBeInTheDocument();
		expect(screen.queryByText(/FinGreenOps/)).not.toBeInTheDocument();
	});
});
