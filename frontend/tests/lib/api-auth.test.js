/**
 * API client: cookie sessions with CSRF (F14, K11), 401 handling for every
 * helper including mutations (K9, BUG-25), read-only recommendation reads,
 * scan-run endpoints (E1) and preview discard (D6, BUG-48).
 *
 * Uses the real jsdom window so window events and storage are real.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '$lib/api.js';
import { createMemoryStorage, installStorage } from '../helpers/storage.js';

const {
	AUTH_REQUIRED_EVENT,
	PASSWORD_CHANGE_REQUIRED_EVENT,
	applyRecommendation,
	changePassword,
	clearCsrfToken,
	createApiKey,
	discardAutomationPlan,
	downloadReport,
	getActiveRecommendations,
	getAppliedRecommendations,
	getHealth,
	getIgnoredRecommendations,
	getMe,
	getOptimizationRun,
	getRecommendationSavings,
	ignoreRecommendation,
	listOptimizationRuns,
	login,
	logout,
	refreshDashboardSummary,
	requestOptimizationRun,
	revokeApiKey,
	revokeSession,
	unignoreRecommendation
} = api;

const TEST_PASSWORD = ['not', 'a', 'secret'].join('-');

function okResponse(body, { status = 200, headers = {} } = {}) {
	return {
		ok: true,
		status,
		headers: { get: (name) => headers[name] ?? null },
		json: () => Promise.resolve(body),
		blob: () => Promise.resolve(new Blob(['csv']))
	};
}

function errorResponse(status, body = {}) {
	return { ok: false, status, json: () => Promise.resolve(body) };
}

function lastCall() {
	const [url, options] = fetch.mock.calls.at(-1);
	return { url: new URL(url), options };
}

let restoreStorages = [];

beforeEach(() => {
	restoreStorages = [
		installStorage('sessionStorage', createMemoryStorage()),
		installStorage('localStorage', createMemoryStorage())
	];
	clearCsrfToken();
});

afterEach(() => {
	clearCsrfToken();
	restoreStorages.reverse().forEach((restore) => restore());
	vi.restoreAllMocks();
});

describe('cookie sessions (F14)', () => {
	it('signs in with a JSON body and never stores anything in browser storage', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ kind: 'user', name: 'admin', csrf_token: 'csrf-1' })));

		await login({ username: 'admin', password: TEST_PASSWORD, remember: true });

		const { url, options } = lastCall();
		expect(url.pathname).toBe('/api/v1/auth/login');
		expect(options.method).toBe('POST');
		expect(options.credentials).toBe('same-origin');
		expect(JSON.parse(options.body)).toEqual({ username: 'admin', password: TEST_PASSWORD, remember: true });
		expect(options.headers.Authorization).toBeUndefined();
		expect(window.sessionStorage.length).toBe(0);
		expect(window.localStorage.length).toBe(0);
	});

	it('sends the CSRF token learnt from /auth/me on mutations only', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ kind: 'user', csrf_token: 'csrf-me' })));
		await getMe();

		await getHealth();
		expect(lastCall().options.headers['X-CSRF-Token']).toBeUndefined();

		await ignoreRecommendation(7, { reason: null });
		expect(lastCall().options.headers['X-CSRF-Token']).toBe('csrf-me');
		await unignoreRecommendation(7);
		expect(lastCall().options.headers['X-CSRF-Token']).toBe('csrf-me');
	});

	it('refreshes a stale CSRF token once and retries the mutation', async () => {
		const responses = [
			errorResponse(403, { detail: { code: 'CSRF_REJECTED', message: 'rejected' } }),
			okResponse({ kind: 'user', csrf_token: 'csrf-new' }),
			okResponse({ run_id: 1, status: 'queued' })
		];
		globalThis.fetch = vi.fn(() => Promise.resolve(responses.shift()));
		api.setCsrfToken('csrf-old');

		await expect(requestOptimizationRun()).resolves.toEqual({ run_id: 1, status: 'queued' });

		expect(fetch).toHaveBeenCalledTimes(3);
		expect(new URL(fetch.mock.calls[1][0]).pathname).toBe('/api/v1/auth/me');
		expect(lastCall().options.headers['X-CSRF-Token']).toBe('csrf-new');
	});

	it('gives up after one CSRF retry', async () => {
		globalThis.fetch = vi.fn((url) =>
			Promise.resolve(
				new URL(url).pathname === '/api/v1/auth/me'
					? okResponse({ csrf_token: 'csrf-new' })
					: errorResponse(403, { detail: { code: 'CSRF_REJECTED', message: 'rejected' } })
			)
		);

		await expect(requestOptimizationRun()).rejects.toMatchObject({ status: 403, code: 'CSRF_REJECTED' });
		expect(fetch).toHaveBeenCalledTimes(3);
	});

	it('asks for a password change when the API requires one', async () => {
		const listener = vi.fn();
		window.addEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, listener);
		globalThis.fetch = vi.fn(() =>
			Promise.resolve(errorResponse(403, { detail: { code: 'PASSWORD_CHANGE_REQUIRED', message: 'change it' } }))
		);

		await expect(getHealth()).rejects.toMatchObject({ status: 403 });
		expect(listener).toHaveBeenCalledTimes(1);
		window.removeEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, listener);
	});

	it('signs out server-side and forgets the CSRF token', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ csrf_token: 'csrf-1' })));
		await getMe();
		globalThis.fetch = vi.fn(() =>
			Promise.resolve({ ok: true, status: 204, json: () => Promise.reject(new Error('empty')) })
		);

		await logout();
		expect(lastCall().url.pathname).toBe('/api/v1/auth/logout');
		expect(lastCall().options.method).toBe('POST');
		expect(lastCall().options.headers['X-CSRF-Token']).toBe('csrf-1');

		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({})));
		await ignoreRecommendation(1, {});
		expect(lastCall().options.headers['X-CSRF-Token']).toBeUndefined();
	});

	it('maps account and API-key helpers to the auth endpoints', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ csrf_token: 'csrf-2' })));

		await changePassword({ currentPassword: 'old-password-1', newPassword: 'new-password-12' });
		expect(lastCall().url.pathname).toBe('/api/v1/auth/password');
		expect(JSON.parse(lastCall().options.body)).toEqual({
			current_password: 'old-password-1',
			new_password: 'new-password-12'
		});

		await revokeSession('abc/1');
		expect(lastCall().url.pathname).toBe('/api/v1/auth/sessions/abc%2F1');
		expect(lastCall().options.method).toBe('DELETE');
		expect(lastCall().options.headers['X-CSRF-Token']).toBe('csrf-2');

		await createApiKey({ name: 'prom', scopes: ['metrics:read'], expiresInDays: '30' });
		expect(lastCall().url.pathname).toBe('/api/v1/auth/api-keys');
		expect(JSON.parse(lastCall().options.body)).toEqual({ name: 'prom', scopes: ['metrics:read'], expires_in_days: 30 });

		await revokeApiKey('abcd1234');
		expect(lastCall().url.pathname).toBe('/api/v1/auth/api-keys/abcd1234');
	});

	it('no longer exposes the per-tab API key flow (F13 replaced) or runtime config writes (A13)', () => {
		expect(api.setApiToken).toBeUndefined();
		expect(api.API_TOKEN_STORAGE_KEY).toBeUndefined();
		expect(api.updateServiceConfig).toBeUndefined();
	});
});

describe('mutation helpers route through request() (K9, BUG-25)', () => {
	const mutations = [
		['applyRecommendation', () => applyRecommendation(7, {})],
		['ignoreRecommendation', () => ignoreRecommendation(7, { reason: null })],
		['unignoreRecommendation', () => unignoreRecommendation(7)],
		['refreshDashboardSummary', () => refreshDashboardSummary({ namespace: 'prod' })],
		['downloadReport', () => downloadReport({ format: 'csv' })]
	];

	it.each(mutations)('%s opens the sign-in view on 401', async (_name, call) => {
		const listener = vi.fn();
		window.addEventListener(AUTH_REQUIRED_EVENT, listener);
		globalThis.fetch = vi.fn(() => Promise.resolve(errorResponse(401, { detail: 'Unauthorized' })));

		await expect(call()).rejects.toMatchObject({ name: 'ApiError', status: 401 });

		expect(listener).toHaveBeenCalledTimes(1);
		window.removeEventListener(AUTH_REQUIRED_EVENT, listener);
	});

	it.each(mutations)('%s sends same-origin credentials', async (_name, call) => {
		URL.createObjectURL = vi.fn(() => 'blob:report');
		URL.revokeObjectURL = vi.fn();
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({})));

		await call();

		expect(lastCall().options.credentials).toBe('same-origin');
		expect(lastCall().options.headers.Authorization).toBeUndefined();
	});

	it('keeps the JSON bodies and methods of the recommendation mutations', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ id: 7 })));

		await ignoreRecommendation(7, { reason: 'burst workload' });
		expect(lastCall().url.pathname).toBe('/api/v1/recommendations/7/ignore');
		expect(lastCall().options.method).toBe('PATCH');
		expect(lastCall().options.headers['Content-Type']).toBe('application/json');
		expect(JSON.parse(lastCall().options.body)).toEqual({ reason: 'burst workload' });

		await unignoreRecommendation(7);
		expect(lastCall().options.method).toBe('DELETE');

		await refreshDashboardSummary({});
		expect(lastCall().url.search).toBe('');
		expect(lastCall().options.method).toBe('POST');
	});
});

describe('recommendation reads (K9)', () => {
	it('never asks the server to scan when reading active recommendations', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse([])));
		const controller = new AbortController();

		await getActiveRecommendations({
			namespace: 'payments',
			source: 'vpa',
			risk_level: 'low',
			capability: 'cpu_rightsizing',
			signal: controller.signal
		});

		const { url, options } = lastCall();
		expect(url.pathname).toBe('/api/v1/recommendations/active');
		expect(url.searchParams.get('namespace')).toBe('payments');
		expect(url.searchParams.get('source')).toBe('vpa');
		expect(url.searchParams.get('risk_level')).toBe('low');
		expect(url.searchParams.get('capability')).toBe('cpu_rightsizing');
		expect(url.searchParams.has('refresh')).toBe(false);
		expect(options.signal).toBe(controller.signal);
	});

	it('scopes ignored, applied and savings reads to the selected namespace', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse([])));
		const { signal } = new AbortController();

		await getIgnoredRecommendations({ namespace: 'payments', signal });
		expect(lastCall().url.searchParams.get('namespace')).toBe('payments');
		expect(lastCall().options.signal).toBe(signal);

		await getAppliedRecommendations({ namespace: 'payments', signal });
		expect(lastCall().url.pathname).toBe('/api/v1/recommendations/applied');
		expect(lastCall().url.searchParams.get('namespace')).toBe('payments');

		await getRecommendationSavings({ namespace: 'payments', signal });
		expect(lastCall().options.signal).toBe(signal);
	});
});

describe('recommendation scan runs (E1)', () => {
	it('requests a run with POST and reads its status', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ run_id: 12, status: 'queued' }, { status: 202 })));

		await expect(requestOptimizationRun()).resolves.toEqual({ run_id: 12, status: 'queued' });
		expect(lastCall().url.pathname).toBe('/api/v1/optimization/runs');
		expect(lastCall().options.method).toBe('POST');

		await getOptimizationRun(12);
		expect(lastCall().url.pathname).toBe('/api/v1/optimization/runs/12');
		expect(lastCall().options.method).toBeUndefined();
	});

	it('lists recent runs from an array or an envelope', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse([{ run_id: 3 }])));
		await expect(listOptimizationRuns({ limit: 1 })).resolves.toEqual([{ run_id: 3 }]);
		expect(lastCall().url.searchParams.get('limit')).toBe('1');

		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ items: [{ run_id: 4 }] })));
		await expect(listOptimizationRuns()).resolves.toEqual([{ run_id: 4 }]);
	});
});

describe('discardAutomationPlan (D6, BUG-48)', () => {
	it('deletes the preview and accepts an empty 204 response', async () => {
		globalThis.fetch = vi.fn(() =>
			Promise.resolve({ ok: true, status: 204, json: () => Promise.reject(new Error('no body')) })
		);

		await expect(discardAutomationPlan('pln/1')).resolves.toEqual({ discarded: true, status: 204 });
		expect(lastCall().url.pathname).toBe('/api/v1/automation/plans/pln%2F1');
		expect(lastCall().options.method).toBe('DELETE');
	});

	it('reports the status the server answered', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ plan_id: 'pln-1', status: 'superseded' })));

		await expect(discardAutomationPlan('pln-1')).resolves.toEqual({ discarded: true, status: 200 });
	});

	it.each([404, 405])(
		'never falls back to the cancel endpoint, which also cancels approved plans (%s)',
		async (status) => {
			globalThis.fetch = vi.fn(() => Promise.resolve(errorResponse(status, { detail: 'Not supported' })));

			await expect(discardAutomationPlan('pln-1')).rejects.toMatchObject({ status });
			expect(fetch).toHaveBeenCalledTimes(1);
			expect(lastCall().url.pathname).toBe('/api/v1/automation/plans/pln-1');
			expect(lastCall().options.method).toBe('DELETE');
		}
	);

	it('reports a plan that can no longer be discarded', async () => {
		globalThis.fetch = vi.fn(() =>
			Promise.resolve(errorResponse(409, { code: 'plan_stale', detail: 'Only draft plans can be discarded.' }))
		);

		await expect(discardAutomationPlan('pln-1')).rejects.toMatchObject({ status: 409, code: 'plan_stale' });
		expect(fetch).toHaveBeenCalledTimes(1);
	});
});
