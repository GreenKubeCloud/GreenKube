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
	getRecommendationSavings,
	getReportYears,
	getReportSummary,
	buildReportExportUrl,
	downloadReport,
	getNodesPage,
	getMetricsPage,
	createAutomationPlan,
	getAutomationPlan,
	getAutomationOperation,
	approveAutomationPlan,
	cancelAutomationPlan,
	previewAutomationPlanRollback,
	getGitOpsStatus,
	refreshGitOpsDiscovery,
	getGitOpsCoverage,
	getGitOpsBindings,
	setApiToken,
	clearApiToken
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
	clearApiToken();
	vi.restoreAllMocks();
});


// ---------------------------------------------------------------------------
// getHealth
// ---------------------------------------------------------------------------
describe('getHealth', () => {
	it('uses same-origin credentials and an in-memory bearer token when configured', async () => {
		globalThis.fetch = mockFetchOk({ status: 'ok' });
		setApiToken('short-lived-token');
		await getHealth();
		const [, options] = fetch.mock.calls[0];
		expect(options.credentials).toBe('include');
		expect(options.headers.Authorization).toBe('Bearer short-lived-token');
	});

	it('returns health data on success', async () => {
		globalThis.fetch = mockFetchOk({ status: 'ok', version: '0.2.6' });
		const result = await getHealth();
		expect(result).toEqual({ status: 'ok', version: '0.2.6' });
		expect(fetch).toHaveBeenCalledTimes(1);
	});

	describe('downloadReport', () => {
		it('downloads through fetch so protected exports receive auth', async () => {
			globalThis.URL.createObjectURL = vi.fn(() => 'blob:report');
			globalThis.URL.revokeObjectURL = vi.fn();
			globalThis.fetch = vi.fn(() => Promise.resolve({
				ok: true,
				headers: { get: () => 'attachment; filename="report.csv"' },
				blob: () => Promise.resolve(new Blob(['csv']))
			}));
			await downloadReport({ namespace: 'prod', last: '24h', format: 'csv' });
			expect(fetch.mock.calls[0][1].credentials).toBe('include');
			expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:report');
		});
	});

	it('throws on API error', async () => {
		globalThis.fetch = mockFetchError(500, 'Internal server error');
		await expect(getHealth()).rejects.toThrow('Internal server error');
	});
});

describe('automation plan API', () => {
	it('creates a plan with recommendation IDs only', async () => {
		globalThis.fetch = mockFetchOk({ plan_id: 'pln-1' });

		await createAutomationPlan([42, 43]);

		const [url, options] = fetch.mock.calls[0];
		expect(url).toBe('http://localhost:3000/api/v1/automation/plans');
		expect(options.method).toBe('POST');
		expect(options.headers['Content-Type']).toBe('application/json');
		expect(JSON.parse(options.body)).toEqual({ recommendation_ids: [42, 43] });
	});

	it('sends a one-time values mapping without repository identity or credentials', async () => {
		globalThis.fetch = mockFetchOk({ plan_id: 'pln-1' });
		const mapping = {
			file_path: 'apps/payments/values.yaml',
			container: 'api',
			path: ['app', 'resources']
		};

		await createAutomationPlan([42], mapping);

		const [, options] = fetch.mock.calls[0];
		expect(JSON.parse(options.body)).toEqual({
			recommendation_ids: [42],
			values_mapping: mapping
		});
	});

	it('exposes server-resolved mapping choices on an ambiguous chart error', async () => {
		const mappingOptions = {
			files: ['apps/payments/values.yaml'],
			containers: ['api']
		};
		globalThis.fetch = vi.fn(() => Promise.resolve({
			ok: false,
			status: 422,
			json: () => Promise.resolve({
				code: 'needs_chart_mapping',
				detail: 'Choose a one-time mapping.',
				mapping_options: mappingOptions
			})
		}));

		await expect(createAutomationPlan([42])).rejects.toMatchObject({
			code: 'needs_chart_mapping',
			mappingOptions
		});
	});

	it('approves a plan with the exact digest in the request body', async () => {
		globalThis.fetch = mockFetchOk({ status: 'queued' });

		await approveAutomationPlan('pln/1', 'sha256:exact-digest');

		const [url, options] = fetch.mock.calls[0];
		expect(url).toBe('http://localhost:3000/api/v1/automation/plans/pln%2F1/approve');
		expect(options.method).toBe('POST');
		expect(JSON.parse(options.body)).toEqual({ digest: 'sha256:exact-digest' });
	});

	it('cancels an encoded plan ID without sending an unnecessary body', async () => {
		globalThis.fetch = mockFetchOk({ plan_id: 'pln/1', status: 'stale' });

		await cancelAutomationPlan('pln/1');

		const [url, options] = fetch.mock.calls[0];
		expect(url).toBe('http://localhost:3000/api/v1/automation/plans/pln%2F1/cancel');
		expect(options.method).toBe('POST');
		expect(options.body).toBeUndefined();
	});

	it('requests a rollback preview for an encoded plan ID', async () => {
		globalThis.fetch = mockFetchOk({ plan_id: 'rollback-1', rollback_of_plan_id: 'pln/1' });

		await previewAutomationPlanRollback('pln/1');

		const [url, options] = fetch.mock.calls[0];
		expect(url).toBe('http://localhost:3000/api/v1/automation/plans/pln%2F1/rollback-preview');
		expect(options.method).toBe('POST');
		expect(options.body).toBeUndefined();
	});

	it('reads lifecycle state for one plan using an encoded plan ID', async () => {
		globalThis.fetch = mockFetchOk({ plan_id: 'pln/1', status: 'sync_pending' });

		const result = await getAutomationPlan('pln/1');

		expect(result.status).toBe('sync_pending');
		expect(fetch.mock.calls[0][0]).toBe('http://localhost:3000/api/v1/automation/plans/pln%2F1');
	});

	it('reads execution state for one encoded operation ID', async () => {
		globalThis.fetch = mockFetchOk({
			operation_id: 'operation/1',
			status: 'running',
			result: { plan_status: 'applying' }
		});

		const result = await getAutomationOperation('operation/1');

		expect(result.status).toBe('running');
		expect(fetch.mock.calls[0][0]).toBe(
			'http://localhost:3000/api/v1/automation/operations/operation%2F1'
		);
		expect(fetch.mock.calls[0][1].method).toBeUndefined();
	});
});

describe('GitOps operations API', () => {
	it('reads discovery status from the GitOps status endpoint', async () => {
		const data = { discovery: {}, coverage: {} };
		globalThis.fetch = mockFetchOk(data);

		const result = await getGitOpsStatus();

		expect(result).toEqual(data);
		expect(fetch.mock.calls[0][0]).toBe('http://localhost:3000/api/v1/gitops/status');
		expect(fetch.mock.calls[0][1].method).toBeUndefined();
	});

	it('posts an immediate GitOps discovery refresh request', async () => {
		globalThis.fetch = mockFetchOk({ cluster: 'minikube', coverage: {} });

		await refreshGitOpsDiscovery({ cluster: 'minikube' });

		const [requestUrl, options] = fetch.mock.calls[0];
		const url = new URL(requestUrl);
		expect(url.pathname).toBe('/api/v1/gitops/refresh');
		expect(url.searchParams.get('cluster')).toBe('minikube');
		expect(options.method).toBe('POST');
		expect(options.credentials).toBe('include');
	});

	it('reads persisted workload coverage from the GitOps coverage endpoint', async () => {
		const data = { total: 3, resolved: 2, ratio: 2 / 3 };
		globalThis.fetch = mockFetchOk(data);

		const result = await getGitOpsCoverage();

		expect(result).toEqual(data);
		expect(fetch.mock.calls[0][0]).toBe('http://localhost:3000/api/v1/gitops/coverage');
		expect(fetch.mock.calls[0][1].method).toBeUndefined();
	});

	it('includes blocked and stale bindings when listing actionable GitOps bindings', async () => {
		globalThis.fetch = mockFetchOk({ items: [] });

		await getGitOpsBindings();

		const url = new URL(fetch.mock.calls[0][0]);
		expect(url.pathname).toBe('/api/v1/gitops/bindings');
		expect(url.searchParams.getAll('status')).toEqual([
			'ambiguous',
			'unresolved',
			'blocked',
			'stale'
		]);
		expect(url.searchParams.get('limit')).toBe('100');
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

	it('exposes the configuration acknowledgement without changing the response shape', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve({
			ok: true,
			headers: { get: (name) => name === 'X-Configuration-Version' ? '12' : 'true' },
			json: () => Promise.resolve({ services: {} })
		}));
		const result = await updateServiceConfig({ prometheus_url: 'http://prom:9090' });
		expect(result.services).toEqual({});
		expect(result.configurationAcknowledgement).toEqual({ version: '12', persisted: true });
		expect(Object.keys(result)).not.toContain('configurationAcknowledgement');
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
// getRecommendationSavings
// ---------------------------------------------------------------------------
describe('getRecommendationSavings', () => {
	it('passes namespace and last as query params', async () => {
		globalThis.fetch = mockFetchOk({ total_cost_saved: 0 });
		await getRecommendationSavings({ namespace: 'prod', last: '7d' });

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('/api/v1/recommendations/savings');
		expect(url).toContain('namespace=prod');
		expect(url).toContain('last=7d');
	});
});


// ---------------------------------------------------------------------------
// getReportSummary
// ---------------------------------------------------------------------------
describe('getReportSummary', () => {
	it('passes all filter params', async () => {
		globalThis.fetch = mockFetchOk({ total_rows: 10 });
		await getReportSummary({ namespace: 'prod', last: '30d', aggregate: true, granularity: 'daily', group_by: 'namespace' });

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('namespace=prod');
		expect(url).toContain('last=30d');
		expect(url).toContain('aggregate=true');
		expect(url).toContain('granularity=daily');
		expect(url).toContain('group_by=namespace');
	});

	it('passes custom dates and selected years as query params', async () => {
		globalThis.fetch = mockFetchOk({ total_rows: 10 });
		await getReportSummary({ start: '2025-01-01', end: '2025-12-31', years: [2025, 2026] });

		const url = fetch.mock.calls[0][0];
		expect(url).toContain('start=2025-01-01');
		expect(url).toContain('end=2025-12-31');
		expect(url.match(/years=/g)).toHaveLength(2);
		expect(url).toContain('years=2025');
		expect(url).toContain('years=2026');
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
// getReportYears
// ---------------------------------------------------------------------------
describe('getReportYears', () => {
	it('returns available report years', async () => {
		globalThis.fetch = mockFetchOk([2026, 2025]);
		const result = await getReportYears({ namespace: 'prod' });

		const url = fetch.mock.calls[0][0];
		expect(result).toEqual([2026, 2025]);
		expect(url).toContain('/api/v1/report/years');
		expect(url).toContain('namespace=prod');
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
			start: '2025-01-01',
			end: '2025-12-31',
			years: [2025, 2026],
			aggregate: true,
			granularity: 'daily',
			group_by: 'namespace',
			format: 'csv'
		});
		expect(url).toContain('/api/v1/report/export');
		expect(url).toContain('namespace=prod');
		expect(url).toContain('last=7d');
		expect(url).toContain('start=2025-01-01');
		expect(url).toContain('end=2025-12-31');
		expect(url.match(/years=/g)).toHaveLength(2);
		expect(url).toContain('aggregate=true');
		expect(url).toContain('granularity=daily');
		expect(url).toContain('group_by=namespace');
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

	it('preserves actionable error metadata', async () => {
		globalThis.fetch = vi.fn(() => Promise.resolve({
			ok: false,
			status: 429,
			json: () => Promise.resolve({
				detail: 'Rate limited',
				code: 'provider_not_ready',
				action: 'Wait and retry',
				retry_after: 3,
				policy_result: { allowed: false, checks: [], rejections: [] }
			})
		}));
		await expect(getHealth()).rejects.toMatchObject({
			name: 'ApiError',
			status: 429,
			code: 'provider_not_ready',
			action: 'Wait and retry',
			retryable: true,
			retryAfter: 3,
			policyResult: { allowed: false, checks: [], rejections: [] }
		});
	});

	it('keeps authentication request-scoped and never stores it in browser storage', async () => {
		setApiToken('ephemeral-token');
		globalThis.fetch = mockFetchOk({});
		await getHealth();
		expect(fetch.mock.calls[0][1].headers.Authorization).toBe('Bearer ephemeral-token');
		expect(fetch.mock.calls[0][1].credentials).toBe('include');
		clearApiToken();
	});
});

describe('cursor contracts', () => {
	it('passes opaque cursors unchanged for paginated resources', async () => {
		globalThis.fetch = mockFetchOk({ items: [], next_cursor: 'opaque/+/=' });
		const nodes = await getNodesPage({ limit: 2, cursor: 'opaque/+/=' });
		expect(nodes.next_cursor).toBe('opaque/+/=');
		const nodeUrl = fetch.mock.calls[0][0];
		expect(nodeUrl).toContain('limit=2');
		expect(nodeUrl).toContain('cursor=opaque%2F%2B%2F%3D');

		await getMetricsPage({ namespace: 'prod', limit: 1, cursor: 'next' });
		expect(fetch.mock.calls[1][0]).toContain('cursor=next');
	});
});
