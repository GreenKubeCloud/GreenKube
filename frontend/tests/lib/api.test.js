/**
 * Tests for the API client module.
 *
 * All fetch calls are mocked — no real HTTP requests are made.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
	getHealth,
	getServicesHealth,
	getServiceHealth,
	updateServiceConfig,
	getVersion,
	getConfig,
	getNamespaces,
	getMetrics,
	getMetricsSummary,
	getTimeseries,
	getNodes,
	getRecommendations,
	getReportSummary,
	buildReportExportUrl
} from '$lib/api.js';


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function mockFetchOk(data) {
	return vi.fn(() =>
		Promise.resolve({
			ok: true,
			json: () => Promise.resolve(data)
		})
	);
}

function mockFetchError(status, detail = '') {
	return vi.fn(() =>
		Promise.resolve({
			ok: false,
			status,
			json: () => Promise.resolve({ detail: detail || `API error ${status}` })
		})
	);
}

beforeEach(() => {
	// Provide window.location.origin for URL construction in the API module
	delete globalThis.window;
	globalThis.window = { location: { origin: 'http://localhost:3000' } };
});

afterEach(() => {
	vi.restoreAllMocks();
});


// ---------------------------------------------------------------------------
// getHealth
// ---------------------------------------------------------------------------
describe('getHealth', () => {
	it('returns health data on success', async () => {
		globalThis.fetch = mockFetchOk({ status: 'ok', version: '0.2.6' });
		const result = await getHealth();
		expect(result).toEqual({ status: 'ok', version: '0.2.6' });
		expect(fetch).toHaveBeenCalledTimes(1);
	});

	it('throws on API error', async () => {
		globalThis.fetch = mockFetchError(500, 'Internal server error');
		await expect(getHealth()).rejects.toThrow('Internal server error');
	});
});


// ---------------------------------------------------------------------------
// getServicesHealth
// ---------------------------------------------------------------------------
describe('getServicesHealth', () => {
	it('calls without force param by default', async () => {
		const data = { status: 'ok', services: {} };
		globalThis.fetch = mockFetchOk(data);
		await getServicesHealth();

		const url = fetch.mock.calls[0][0];
		expect(url).not.toContain('force=');
	});

	it('passes force=true when requested', async () => {
		const data = { status: 'ok', services: {} };
		globalThis.fetch = mockFetchOk(data);
		await getServicesHealth(true);

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('force=true');
	});
});


// ---------------------------------------------------------------------------
// getServiceHealth
// ---------------------------------------------------------------------------
describe('getServiceHealth', () => {
	it('requests the correct service path', async () => {
		globalThis.fetch = mockFetchOk({ status: 'healthy' });
		await getServiceHealth('prometheus');

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('/health/services/prometheus');
	});
});


// ---------------------------------------------------------------------------
// updateServiceConfig
// ---------------------------------------------------------------------------
describe('updateServiceConfig', () => {
	it('sends POST with JSON body', async () => {
		globalThis.fetch = vi.fn(() =>
			Promise.resolve({
				ok: true,
				json: () => Promise.resolve({ updated: true })
			})
		);

		const config = { prometheus_url: 'http://prom:9090' };
		const result = await updateServiceConfig(config);
		expect(result).toEqual({ updated: true });

		const [url, opts] = fetch.mock.calls[0];
		expect(url).toContain('/config/services');
		expect(opts.method).toBe('POST');
		expect(opts.headers['Content-Type']).toBe('application/json');
		expect(JSON.parse(opts.body)).toEqual(config);
	});

	it('throws on error response', async () => {
		globalThis.fetch = vi.fn(() =>
			Promise.resolve({
				ok: false,
				status: 400,
				json: () => Promise.resolve({ detail: 'Invalid config' })
			})
		);
		await expect(updateServiceConfig({})).rejects.toThrow('Invalid config');
	});
});


// ---------------------------------------------------------------------------
// getVersion
// ---------------------------------------------------------------------------
describe('getVersion', () => {
	it('returns version object', async () => {
		globalThis.fetch = mockFetchOk({ version: '0.2.6' });
		const result = await getVersion();
		expect(result.version).toBe('0.2.6');
	});
});


// ---------------------------------------------------------------------------
// getConfig
// ---------------------------------------------------------------------------
describe('getConfig', () => {
	it('returns config data', async () => {
		const data = { db_type: 'postgres', log_level: 'INFO' };
		globalThis.fetch = mockFetchOk(data);
		const result = await getConfig();
		expect(result).toEqual(data);
	});
});


// ---------------------------------------------------------------------------
// getNamespaces
// ---------------------------------------------------------------------------
describe('getNamespaces', () => {
	it('returns an array of namespace strings', async () => {
		globalThis.fetch = mockFetchOk(['default', 'production', 'monitoring']);
		const result = await getNamespaces();
		expect(result).toEqual(['default', 'production', 'monitoring']);
	});
});


// ---------------------------------------------------------------------------
// getMetrics
// ---------------------------------------------------------------------------
describe('getMetrics', () => {
	it('unwraps items array from response', async () => {
		const metrics = [{ pod_name: 'pod-a', co2e_grams: 10 }];
		globalThis.fetch = mockFetchOk({ items: metrics });
		const result = await getMetrics({});
		expect(result).toEqual(metrics);
	});

	it('falls back to raw array if no items wrapper', async () => {
		const metrics = [{ pod_name: 'pod-a', co2e_grams: 10 }];
		globalThis.fetch = mockFetchOk(metrics);
		const result = await getMetrics({});
		expect(result).toEqual(metrics);
	});

	it('passes namespace and last as query params', async () => {
		globalThis.fetch = mockFetchOk({ items: [] });
		await getMetrics({ namespace: 'prod', last: '7d' });

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('namespace=prod');
		expect(url).toContain('last=7d');
	});

	it('skips empty params', async () => {
		globalThis.fetch = mockFetchOk({ items: [] });
		await getMetrics({ namespace: '', last: null });

		const url = fetch.mock.calls[0][0];
		expect(url).not.toContain('namespace=');
		expect(url).not.toContain('last=');
	});
});


// ---------------------------------------------------------------------------
// getMetricsSummary
// ---------------------------------------------------------------------------
describe('getMetricsSummary', () => {
	it('returns summary object', async () => {
		const data = { total_co2e_grams: 500, total_cost: 12.34 };
		globalThis.fetch = mockFetchOk(data);
		const result = await getMetricsSummary({ last: '24h' });
		expect(result).toEqual(data);
	});
});


// ---------------------------------------------------------------------------
// getTimeseries
// ---------------------------------------------------------------------------
describe('getTimeseries', () => {
	it('passes granularity param', async () => {
		globalThis.fetch = mockFetchOk([]);
		await getTimeseries({ granularity: 'hour', last: '7d' });

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('granularity=hour');
		expect(url).toContain('last=7d');
	});
});


// ---------------------------------------------------------------------------
// getNodes
// ---------------------------------------------------------------------------
describe('getNodes', () => {
	it('returns node array', async () => {
		const nodes = [{ name: 'node-1', zone: 'eu-west-1a' }];
		globalThis.fetch = mockFetchOk(nodes);
		const result = await getNodes();
		expect(result).toEqual(nodes);
	});
});


// ---------------------------------------------------------------------------
// getRecommendations
// ---------------------------------------------------------------------------
describe('getRecommendations', () => {
	it('passes namespace when provided', async () => {
		globalThis.fetch = mockFetchOk([]);
		await getRecommendations({ namespace: 'staging' });

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('namespace=staging');
	});

	it('skips namespace when not provided', async () => {
		globalThis.fetch = mockFetchOk([]);
		await getRecommendations({});

		const url = fetch.mock.calls[0][0];
		expect(url).not.toContain('namespace=');
	});
});


// ---------------------------------------------------------------------------
// getReportSummary
// ---------------------------------------------------------------------------
describe('getReportSummary', () => {
	it('passes all filter params', async () => {
		globalThis.fetch = mockFetchOk({ total_rows: 10 });
		await getReportSummary({ namespace: 'prod', last: '30d', aggregate: true, granularity: 'daily' });

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('namespace=prod');
		expect(url).toContain('last=30d');
		expect(url).toContain('aggregate=true');
		expect(url).toContain('granularity=daily');
	});

	it('omits aggregate and granularity when falsy', async () => {
		globalThis.fetch = mockFetchOk({ total_rows: 0 });
		await getReportSummary({ last: '24h' });

		const url = fetch.mock.calls[0][0];
		expect(url).not.toContain('aggregate=');
		expect(url).not.toContain('granularity=');
	});
});


// ---------------------------------------------------------------------------
// buildReportExportUrl
// ---------------------------------------------------------------------------
describe('buildReportExportUrl', () => {
	it('builds URL with all params', () => {
		const url = buildReportExportUrl({
			namespace: 'prod',
			last: '7d',
			aggregate: true,
			granularity: 'daily',
			format: 'csv'
		});
		expect(url).toContain('/api/v1/report/export');
		expect(url).toContain('namespace=prod');
		expect(url).toContain('last=7d');
		expect(url).toContain('aggregate=true');
		expect(url).toContain('granularity=daily');
		expect(url).toContain('format=csv');
	});

	it('omits undefined/falsy params', () => {
		const url = buildReportExportUrl({ format: 'json' });
		expect(url).toContain('format=json');
		expect(url).not.toContain('namespace=');
		expect(url).not.toContain('last=');
		expect(url).not.toContain('aggregate=');
		expect(url).not.toContain('granularity=');
	});

	it('returns a valid URL string', () => {
		const url = buildReportExportUrl({});
		expect(() => new URL(url)).not.toThrow();
	});
});


// ---------------------------------------------------------------------------
// request() edge cases (via public functions)
// ---------------------------------------------------------------------------
describe('request error handling', () => {
	it('falls back to status code when detail is missing', async () => {
		globalThis.fetch = vi.fn(() =>
			Promise.resolve({
				ok: false,
				status: 503,
				json: () => Promise.reject(new Error('not json'))
			})
		);
		await expect(getHealth()).rejects.toThrow('API error 503');
	});
});
