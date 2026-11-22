/**
 * Setup banner (F1, K6): shown on every page until GET /setup/status says
 * the setup is complete, dismissible for the tab session, and silent when
 * the server does not report a setup status.
 *
 * Uses the real API client with a mocked fetch that emulates the server.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import Layout from '../../src/routes/+layout.svelte';
import { clearCsrfToken } from '#lib/api.js';
import { currentUser } from '#lib/stores.js';
import { SETUP_BANNER_DISMISSED_KEY, setupBannerDismissed, setupStatus } from '#lib/setup.js';
import { createMemoryStorage, installStorage } from '../helpers/storage.js';
import INCOMPLETE_FIXTURE from '../fixtures/api/setup_status_incomplete.json';
import PAUSED_FIXTURE from '../fixtures/api/setup_status_paused.json';
import COMPLETE_FIXTURE from '../fixtures/api/setup_status_complete.json';

const ADMIN = {
	authenticated: true,
	kind: 'user',
	name: 'admin',
	actor: 'user:admin',
	role: 'admin',
	scopes: ['admin', 'read', 'write'],
	must_change_password: false,
	csrf_token: 'csrf-1'
};

// Generated from the FastAPI app (tests/api/test_ui_contract_fixtures.py).
const INCOMPLETE = INCOMPLETE_FIXTURE;

function ok(body, status = 200) {
	return { ok: true, status, json: () => Promise.resolve(body) };
}

function fail(status, body = {}) {
	return { ok: false, status, json: () => Promise.resolve(body) };
}

function installServer(setup) {
	globalThis.fetch = vi.fn((url) => {
		const path = new URL(url).pathname;
		if (path === '/api/v1/health') return Promise.resolve(ok({ status: 'ok', version: '0.4.0' }));
		if (path === '/api/v1/auth/me') return Promise.resolve(ok(ADMIN));
		if (path === '/api/v1/health/services') return Promise.resolve(ok({ status: 'ok', services: {} }));
		if (path === '/api/v1/setup/status') {
			return Promise.resolve(setup === 404 ? fail(404, { detail: 'Not Found' }) : ok(setup));
		}
		return Promise.resolve(ok({}));
	});
}

let restore = [];
let sessionStorage;

beforeEach(() => {
	sessionStorage = createMemoryStorage();
	restore = [installStorage('sessionStorage', sessionStorage), installStorage('localStorage', createMemoryStorage())];
	clearCsrfToken();
	currentUser.set(null);
	setupStatus.set(null);
	setupBannerDismissed.set(false);
});

afterEach(() => {
	clearCsrfToken();
	restore.reverse().forEach((undo) => undo());
	vi.restoreAllMocks();
});

describe('setup banner', () => {
	it('shows how many checks need attention and links the checklist', async () => {
		installServer(INCOMPLETE);
		render(Layout);

		const banner = await screen.findByTestId('setup-banner');
		expect(banner).toHaveTextContent('Setup is not complete.');
		expect(banner).toHaveTextContent('4 checks need your attention');
		expect(screen.getByRole('link', { name: 'Open the setup checklist' })).toHaveAttribute('href', '/setup');
	});

	it('is dismissible for the tab session', async () => {
		installServer(INCOMPLETE);
		render(Layout);

		await fireEvent.click(await screen.findByRole('button', { name: 'Dismiss the setup banner' }));

		await waitFor(() => expect(screen.queryByTestId('setup-banner')).not.toBeInTheDocument());
		expect(sessionStorage.getItem(SETUP_BANNER_DISMISSED_KEY)).toBe('true');
	});

	it('says when delivery is paused by policy', async () => {
		installServer(PAUSED_FIXTURE);
		render(Layout);

		const banner = await screen.findByTestId('setup-banner');
		expect(banner).toHaveTextContent('Delivery is paused by policy (automation.policy.pauseDelivery)');
	});

	it('is hidden once the setup is complete', async () => {
		installServer(COMPLETE_FIXTURE);
		render(Layout);

		await waitFor(() => expect(get(setupStatus)?.complete).toBe(true));
		expect(screen.queryByTestId('setup-banner')).not.toBeInTheDocument();
	});

	it('stays silent on a server without the setup endpoint', async () => {
		installServer(404);
		setupStatus.set({ complete: false, checks: [], capabilities: [], actionCount: 0 });
		render(Layout);

		// The stale status is cleared: no banner rather than a wrong one.
		await waitFor(() => expect(get(setupStatus)).toBeNull());
		expect(screen.queryByTestId('setup-banner')).not.toBeInTheDocument();
	});

	it('adds the overview, ledger, sources and setup entries to the navigation', async () => {
		installServer({ ...INCOMPLETE, complete: true });
		render(Layout);

		for (const [name, href] of [
			['Overview', '/'],
			['Ledger', '/ledger'],
			['Sources', '/sources'],
			['Setup', '/setup']
		]) {
			expect(await screen.findByRole('link', { name })).toHaveAttribute('href', href);
		}
		expect(screen.getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page');
	});
});
