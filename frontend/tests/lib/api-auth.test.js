/**
 * API client: per-tab key persistence (F13, BUG-63), 401 handling for every
 * helper including mutations (K9, BUG-25), read-only recommendation reads,
 * scan-run endpoints (E1) and preview discard (D6, BUG-48).
 *
 * Uses the real jsdom window so sessionStorage and window events are real.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '$lib/api.js';
import { createMemoryStorage, installBlockedStorage, installStorage } from '../helpers/storage.js';

const {
	API_TOKEN_STORAGE_KEY,
	AUTH_REQUIRED_EVENT,
	applyRecommendation,
	clearApiToken,
	discardAutomationPlan,
	downloadReport,
	getActiveRecommendations,
	getAppliedRecommendations,
	getHealth,
	getIgnoredRecommendations,
	getOptimizationRun,
	getRecommendationSavings,
	hasApiToken,
	ignoreRecommendation,
	listOptimizationRuns,
	refreshDashboardSummary,
	requestOptimizationRun,
	restoreApiToken,
	setApiToken,
	unignoreRecommendation,
	updateServiceConfig
} = api;

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
	clearApiToken();
});

afterEach(() => {
	clearApiToken();
	restoreStorages.reverse().forEach((restore) => restore());
	vi.restoreAllMocks();
});

describe('API key persistence (F13)', () => {
	it('keeps the key in sessionStorage for this tab and never in localStorage', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ status: 'ok' })));

		setApiToken('  gk-secret  ');
		await getHealth();

		expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBe('gk-secret');
		expect(window.localStorage.length).toBe(0);
		expect(lastCall().options.headers.Authorization).toBe('Bearer gk-secret');
		expect(hasApiToken()).toBe(true);
	});

	it('restores the key after a reload of the same tab', async () => {
		window.sessionStorage.setItem(API_TOKEN_STORAGE_KEY, 'gk-from-previous-load');
		vi.resetModules();
		const reloaded = await import('$lib/api.js');
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ status: 'ok' })));

		await reloaded.getHealth();

		expect(reloaded.hasApiToken()).toBe(true);
		expect(lastCall().options.headers.Authorization).toBe('Bearer gk-from-previous-load');
		reloaded.clearApiToken();
	});

	it('restoreApiToken re-reads the stored key without wiping an in-memory key', () => {
		window.sessionStorage.setItem(API_TOKEN_STORAGE_KEY, 'gk-stored');
		expect(restoreApiToken()).toBe('gk-stored');

		window.sessionStorage.clear();
		expect(restoreApiToken()).toBe('gk-stored');
	});

	it('forgets the key on sign out', () => {
		setApiToken('gk-secret');
		clearApiToken();

		expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
		expect(hasApiToken()).toBe(false);
	});

	it.each([
		['throws on every call', () => installStorage('sessionStorage', createMemoryStorage({ failing: true }))],
		['is blocked for the document', () => installBlockedStorage('sessionStorage')]
	])('keeps working in memory when sessionStorage %s', async (_label, install) => {
		restoreStorages.push(install());
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({ status: 'ok' })));

		expect(() => setApiToken('gk-memory-only')).not.toThrow();
		expect(restoreApiToken()).toBe('gk-memory-only');
		await getHealth();

		expect(lastCall().options.headers.Authorization).toBe('Bearer gk-memory-only');
		expect(() => clearApiToken()).not.toThrow();
		expect(hasApiToken()).toBe(false);
	});

	it('clears a rejected key and asks for authentication on 401', async () => {
		const listener = vi.fn();
		window.addEventListener(AUTH_REQUIRED_EVENT, listener);
		globalThis.fetch = vi.fn(() => Promise.resolve(errorResponse(401, { detail: 'Invalid API key' })));
		setApiToken('gk-rotated');

		await expect(getHealth()).rejects.toMatchObject({ status: 401 });

		expect(listener).toHaveBeenCalledTimes(1);
		expect(hasApiToken()).toBe(false);
		expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
		window.removeEventListener(AUTH_REQUIRED_EVENT, listener);
	});

	it('does not forget a newer key when a request sent with an older key is rejected', async () => {
		let reject;
		globalThis.fetch = vi.fn(
			() =>
				new Promise((resolve) => {
					reject = () => resolve(errorResponse(401));
				})
		);
		setApiToken('gk-old');
		const pending = getHealth();
		setApiToken('gk-new');
		reject();

		await expect(pending).rejects.toMatchObject({ status: 401 });
		expect(window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBe('gk-new');
	});
});

describe('mutation helpers route through request() (K9, BUG-25)', () => {
	const mutations = [
		['updateServiceConfig', () => updateServiceConfig({ prometheus_url: 'http://prom:9090' })],
		['applyRecommendation', () => applyRecommendation(7, {})],
		['ignoreRecommendation', () => ignoreRecommendation(7, { reason: null })],
		['unignoreRecommendation', () => unignoreRecommendation(7)],
		['refreshDashboardSummary', () => refreshDashboardSummary({ namespace: 'prod' })],
		['downloadReport', () => downloadReport({ format: 'csv' })]
	];

	it.each(mutations)('%s opens the authentication dialog on 401', async (_name, call) => {
		const listener = vi.fn();
		window.addEventListener(AUTH_REQUIRED_EVENT, listener);
		globalThis.fetch = vi.fn(() => Promise.resolve(errorResponse(401, { detail: 'Unauthorized' })));
		setApiToken('gk-secret');

		await expect(call()).rejects.toMatchObject({ name: 'ApiError', status: 401 });

		expect(listener).toHaveBeenCalledTimes(1);
		expect(hasApiToken()).toBe(false);
		window.removeEventListener(AUTH_REQUIRED_EVENT, listener);
	});

	it.each(mutations)('%s sends the bearer key and same-origin credentials', async (_name, call) => {
		URL.createObjectURL = vi.fn(() => 'blob:report');
		URL.revokeObjectURL = vi.fn();
		globalThis.fetch = vi.fn(() => Promise.resolve(okResponse({})));
		setApiToken('gk-secret');

		await call();

		expect(lastCall().options.headers.Authorization).toBe('Bearer gk-secret');
		expect(lastCall().options.credentials).toBe('include');
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

	it('falls back to the cancel endpoint on a server without DELETE', async () => {
		globalThis.fetch = vi
			.fn()
			.mockResolvedValueOnce(errorResponse(405, { detail: 'Method Not Allowed' }))
			.mockResolvedValueOnce(okResponse({ plan_id: 'pln-1', status: 'stale' }));

		await expect(discardAutomationPlan('pln-1')).resolves.toEqual({ discarded: true, status: 405 });
		expect(lastCall().url.pathname).toBe('/api/v1/automation/plans/pln-1/cancel');
		expect(lastCall().options.method).toBe('POST');
	});

	it('reports a plan that can no longer be discarded', async () => {
		globalThis.fetch = vi.fn(() =>
			Promise.resolve(errorResponse(409, { code: 'plan_stale', detail: 'Only draft plans can be discarded.' }))
		);

		await expect(discardAutomationPlan('pln-1')).rejects.toMatchObject({ status: 409, code: 'plan_stale' });
		expect(fetch).toHaveBeenCalledTimes(1);
	});
});
