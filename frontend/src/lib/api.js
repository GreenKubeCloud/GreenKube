/**
 * GreenKube API client.
 * 
 * The API base URL is resolved at runtime:
 * - In development, Vite proxies /api to localhost:8000
 * - In production (K8s), NGINX proxies /api to the greenkube-api service
 */

const BASE = '/api/v1';

/**
 * sessionStorage key holding the API key for the current browser tab.
 *
 * Stopgap until server-side sessions (F14): the key survives reloads of the
 * same tab and is forgotten when the tab closes. It is never written to
 * localStorage, which would keep a long-lived admin credential on disk.
 */
export const API_TOKEN_STORAGE_KEY = 'greenkube.apiKey';

/** Window event dispatched when the API rejects the current credentials. */
export const AUTH_REQUIRED_EVENT = 'greenkube-auth-required';

function sessionStore() {
	try {
		return typeof window !== 'undefined' ? window.sessionStorage ?? null : null;
	} catch {
		// Accessing sessionStorage throws when site data is blocked.
		return null;
	}
}

function readStoredToken() {
	try {
		const value = sessionStore()?.getItem(API_TOKEN_STORAGE_KEY);
		return typeof value === 'string' ? value : '';
	} catch {
		return '';
	}
}

function writeStoredToken(token) {
	try {
		const storage = sessionStore();
		if (!storage) return;
		if (token) storage.setItem(API_TOKEN_STORAGE_KEY, token);
		else storage.removeItem(API_TOKEN_STORAGE_KEY);
	} catch {
		// Storage can be full, disabled or blocked: keep the in-memory key.
	}
}

let apiToken = readStoredToken();

/** Set the bearer token for this tab: kept in memory and in sessionStorage. */
export function setApiToken(token) {
	apiToken = typeof token === 'string' ? token.trim() : '';
	writeStoredToken(apiToken);
}

/** Forget the bearer token (sign out, or rejected by the API). */
export function clearApiToken() {
	apiToken = '';
	writeStoredToken('');
}

/** Reload the token persisted for this tab, e.g. after a page reload. */
export function restoreApiToken() {
	const stored = readStoredToken();
	if (stored) apiToken = stored;
	return apiToken;
}

/** Whether a bearer token is configured for this tab. */
export function hasApiToken() {
	return Boolean(apiToken);
}

export class ApiError extends Error {
	constructor(
		message,
		{ status, detail, code, action, policyResult, mappingOptions, retryable = false, retryAfter } = {}
	) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
		this.code = code;
		this.action = action;
		this.policyResult = policyResult;
		this.mappingOptions = mappingOptions;
		this.retryable = retryable;
		this.retryAfter = retryAfter;
	}
}

function addSearchParam(url, key, value) {
	if (Array.isArray(value)) {
		value.filter((item) => item !== null && item !== undefined && item !== '').forEach((item) => {
			url.searchParams.append(key, item);
		});
		return;
	}
	if (value !== null && value !== undefined && value !== '') {
		url.searchParams.set(key, value);
	}
}

function requestOptions(options = {}, token = apiToken) {
	const headers = { ...(options.headers || {}) };
	if (token) headers.Authorization = `Bearer ${token}`;
	return { ...options, headers, credentials: 'include' };
}

function jsonOptions(method, body, options = {}) {
	return {
		...options,
		method,
		headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
		body: JSON.stringify(body)
	};
}

function errorDetails(body, status) {
	const detail = typeof body?.detail === 'string'
		? body.detail
		: body?.detail?.message || body?.message;
	return {
		message: detail || `API error ${status}`,
		detail,
		code: body?.code || body?.error_code || body?.detail?.code || body?.detail?.error_code,
		policyResult: body?.policy_result || body?.policyResult || body?.detail?.policy_result,
		mappingOptions: body?.mapping_options || body?.mappingOptions || body?.detail?.mapping_options,
		action: body?.action || body?.detail?.action,
		retryable: body?.retryable === true || status === 408 || status === 429 || status >= 500,
		retryAfter: body?.retry_after || body?.detail?.retry_after
	};
}

function notifyAuthRequired() {
	if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
	try {
		window.dispatchEvent(new CustomEvent(AUTH_REQUIRED_EVENT));
	} catch {
		// A missing CustomEvent implementation must not hide the API error.
	}
}

/**
 * Send one API request and return the raw Response.
 *
 * Every helper goes through here so that a 401 always clears the rejected
 * key and opens the authentication dialog, whatever the HTTP method.
 */
async function send(path, params = {}, options = {}) {
	const url = new URL(path, window.location.origin);
	Object.entries(params).forEach(([k, v]) => {
		addSearchParam(url, k, v);
	});

	const token = apiToken;
	const res = await fetch(url.toString(), requestOptions(options, token));
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		const details = errorDetails(body, res.status);
		// Only react to a rejection of the current credentials: a key entered
		// while this request was in flight must survive a stale 401, and must
		// not reopen the sign-in dialog.
		if (res.status === 401 && apiToken === token) {
			if (token) clearApiToken();
			notifyAuthRequired();
		}
		throw new ApiError(details.message, { status: res.status, ...details });
	}
	return res;
}

async function request(path, params = {}, options = {}) {
	const res = await send(path, params, options);
	if (res.status === 204) return null;
	return res.json();
}

/** @returns {Promise<{status: string, version: string}>} */
export function getHealth() {
	return request(`${BASE}/health`);
}

/** @returns {Promise<{status: string, version: string, services: Object}>} */
export function getServicesHealth(force = false) {
	return request(`${BASE}/health/services`, { force: force || undefined });
}

/**
 * @param {string} serviceName
 * @param {boolean} [force]
 * @returns {Promise<Object>}
 */
export function getServiceHealth(serviceName, force = false) {
	return request(`${BASE}/health/services/${serviceName}`, { force: force || undefined });
}

/**
 * Update service URLs/tokens at runtime.
 * @param {Object} config
 * @param {string} [config.prometheus_url]
 * @param {string} [config.opencost_url]
 * @param {string} [config.electricity_maps_token]
 * @param {string} [config.boavizta_url]
 * @param {string} [config.wattnet_email]
 * @param {string} [config.wattnet_password]
 * @returns {Promise<Object>}
 */
export async function updateServiceConfig(config) {
	const res = await send(`${BASE}/config/services`, {}, jsonOptions('POST', config));
	const data = await res.json();
	Object.defineProperty(data, 'configurationAcknowledgement', {
		value: {
			version: res.headers?.get?.('X-Configuration-Version') || data.configuration_version || null,
			persisted: res.headers?.get?.('X-Configuration-Persisted') === 'true' || data.configuration_persisted === true
		},
		enumerable: false
	});
	return data;
}

/** @returns {Promise<{version: string}>} */
export function getVersion() {
	return request(`${BASE}/version`);
}

/** @returns {Promise<Object>} */
export function getConfig() {
	return request(`${BASE}/config`);
}

/** @returns {Promise<string[]>} */
export function getNamespaces() {
	return request(`${BASE}/namespaces`);
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @returns {Promise<Object[]>}
 */
export async function getMetrics({ namespace, last } = {}) {
	const data = await request(`${BASE}/metrics`, { namespace, last });
	return data.items ?? data;
}

/** Fetch one cursor-paginated metrics page without hiding its envelope. */
export function getMetricsPage({ namespace, start, end, limit, cursor } = {}) {
	return request(`${BASE}/metrics`, { namespace, start, end, limit, cursor });
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @returns {Promise<Object>}
 */
export function getMetricsSummary({ namespace, last, signal } = {}) {
	return request(`${BASE}/metrics/summary`, { namespace, last }, { signal });
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {string} [opts.granularity]
 * @returns {Promise<Object[]>}
 */
export function getTimeseries({ namespace, last, granularity, signal } = {}) {
	return request(`${BASE}/metrics/timeseries`, { namespace, last, granularity }, { signal });
}

/**
 * Lightweight SQL-level aggregation of metrics by namespace.
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @returns {Promise<Object[]>}
 */
export function getMetricsByNamespace({ namespace, last, signal } = {}) {
	return request(`${BASE}/metrics/by-namespace`, { namespace, last }, { signal });
}

/**
 * Lightweight SQL-level aggregation of top pods by CO₂.
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {number} [opts.limit]
 * @returns {Promise<Object[]>}
 */
export function getTopPods({ namespace, last, limit, signal } = {}) {
	return request(`${BASE}/metrics/top-pods`, { namespace, last, limit }, { signal });
}

/** @returns {Promise<Object[]>} */
export function getNodes(options = {}) {
	const { include_inactive, limit, cursor } = options;
	if (limit !== undefined || cursor !== undefined || include_inactive !== undefined) {
		return request(`${BASE}/nodes`, { include_inactive, limit, cursor });
	}
	return request(`${BASE}/nodes`);
}

/** Fetch one cursor-paginated node page. */
export function getNodesPage({ include_inactive, limit = 50, cursor } = {}) {
	return request(`${BASE}/nodes`, { include_inactive, limit, cursor });
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @returns {Promise<Object[]>}
 */
export function getRecommendations({ namespace } = {}) {
	return request(`${BASE}/recommendations`, { namespace });
}

/**
 * Active recommendations in server order (risk-adjusted expected value first),
 * each with its `value` and delivery `eligibility`.
 *
 * The UI never passes `refresh`: a scan is requested explicitly through
 * {@link requestOptimizationRun} so page loads stay read-only (BUG-25).
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.source] — greenkube | vpa | karpenter | …
 * @param {string} [opts.risk_level] — low | medium | high
 * @param {string} [opts.capability] — e.g. cpu_rightsizing
 * @param {string} [opts.profile] — balanced | carbon_first | cost_first | quick_wins | low_risk
 * @param {number} [opts.carbon_price] — carbon price override (currency per tonne CO2e)
 * @param {boolean} [opts.refresh] — deprecated server-side scan trigger
 * @param {AbortSignal} [opts.signal]
 * @returns {Promise<Object[]>}
 */
export function getActiveRecommendations({
	namespace,
	source,
	risk_level,
	capability,
	profile,
	carbon_price,
	refresh,
	signal
} = {}) {
	return request(
		`${BASE}/recommendations/active`,
		{ namespace, source, risk_level, capability, profile, carbon_price, refresh: refresh || undefined },
		{ signal }
	);
}

/**
 * Measured realization rate per source family and capability (Beta(α, β) entries).
 * @param {Object} [opts]
 * @param {AbortSignal} [opts.signal]
 * @returns {Promise<{method: string, prior: Object, entries: Object[]}>}
 */
export function getSourceTrackRecord({ signal } = {}) {
	return request(`${BASE}/sources/track-record`, {}, { signal });
}

/**
 * Ranked active recommendations.
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {number} [opts.limit]
 * @param {'co2'|'cost'|'value'} [opts.metric]
 * @param {string} [opts.profile] — balanced | carbon_first | cost_first | quick_wins | low_risk
 * @returns {Promise<Object[]>}
 */
export function getTopRecommendations({ namespace, limit, metric, profile, refresh, signal } = {}) {
	return request(
		`${BASE}/recommendations/top`,
		{ namespace, limit, metric, profile, refresh: refresh || undefined },
		{ signal }
	);
}

/**
 * Full recommendation detail, including the evidence block.
 * @param {number} id
 * @returns {Promise<Object>}
 */
export function getRecommendation(id) {
	return request(`${BASE}/recommendations/${id}`);
}

/**
 * @param {Object} [opts]
 * @param {string} [opts.namespace]
 * @param {AbortSignal} [opts.signal]
 * @returns {Promise<Object[]>}
 */
export function getIgnoredRecommendations({ namespace, signal } = {}) {
	return request(`${BASE}/recommendations/ignored`, { namespace }, { signal });
}

/**
 * @param {Object} [opts]
 * @param {string} [opts.namespace]
 * @param {AbortSignal} [opts.signal]
 * @returns {Promise<Object[]>}
 */
export function getAppliedRecommendations({ namespace, signal } = {}) {
	return request(`${BASE}/recommendations/applied`, { namespace }, { signal });
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {AbortSignal} [opts.signal]
 * @returns {Promise<Object>}
 */
export function getRecommendationSavings({ namespace, last, signal } = {}) {
	return request(`${BASE}/recommendations/savings`, { namespace, last }, { signal });
}

/**
 * @param {number} id
 * @param {{ carbon_saved_co2e_grams?: number, cost_saved?: number }} [body]
 * @returns {Promise<Object>}
 */
export function applyRecommendation(id, body = {}) {
	return request(`${BASE}/recommendations/${id}/apply`, {}, jsonOptions('PATCH', body));
}

/**
 * @param {number} id
 * @param {{ reason: ?string }} body
 * @returns {Promise<Object>}
 */
export function ignoreRecommendation(id, body) {
	return request(`${BASE}/recommendations/${id}/ignore`, {}, jsonOptions('PATCH', body));
}

/**
 * @param {number} id
 * @returns {Promise<Object>}
 */
export function unignoreRecommendation(id) {
	return request(`${BASE}/recommendations/${id}/ignore`, {}, { method: 'DELETE' });
}

/**
 * Ask the controller for a recommendation scan (E1). The server deduplicates
 * while a run is pending and answers 202 `{run_id, status}`. Older servers
 * answer 404/405; callers then fall back to plain reads.
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{run_id: number, status: string}>}
 */
export function requestOptimizationRun({ signal } = {}) {
	return request(`${BASE}/optimization/runs`, {}, jsonOptions('POST', {}, { signal }));
}

/**
 * Status of one recommendation scan run.
 * @param {number|string} runId
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{run_id: number, status: 'queued'|'running'|'succeeded'|'failed',
 *   requested_at?: string, started_at?: string, completed_at?: string,
 *   recommendation_count?: number, error?: string}>}
 */
export function getOptimizationRun(runId, { signal } = {}) {
	return request(`${BASE}/optimization/runs/${encodeURIComponent(runId)}`, {}, { signal });
}

/**
 * Most recent recommendation scan runs, newest first.
 * @param {{limit?: number, signal?: AbortSignal}} [opts]
 * @returns {Promise<Object[]>}
 */
export async function listOptimizationRuns({ limit, signal } = {}) {
	const data = await request(`${BASE}/optimization/runs`, { limit }, { signal });
	if (Array.isArray(data)) return data;
	return Array.isArray(data?.items) ? data.items : Array.isArray(data?.runs) ? data.runs : [];
}

/**
 * Create a server-resolved, digest-addressed GitOps plan.
 * The optional values mapping contains only a Git-managed file, container, and key path.
 * @param {number[]} recommendationIds
 * @param {{file_path: string, container: string, path: string[]}} [valuesMapping]
 * @returns {Promise<Object>}
 */
export function createAutomationPlan(recommendationIds, valuesMapping = null) {
	const body = { recommendation_ids: recommendationIds };
	if (valuesMapping) body.values_mapping = valuesMapping;
	return request(`${BASE}/automation/plans`, {}, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
}

/**
 * Read the current lifecycle state of a plan.
 * @param {string} planId
 * @returns {Promise<Object>}
 */
export function getAutomationPlan(planId) {
	return request(`${BASE}/automation/plans/${encodeURIComponent(planId)}`);
}

/**
 * Read the execution state of one plan operation.
 * @param {string} operationId
 * @returns {Promise<Object>}
 */
export function getAutomationOperation(operationId) {
	return request(`${BASE}/automation/operations/${encodeURIComponent(operationId)}`);
}

/**
 * Approve a previously previewed plan using the digest returned by the API.
 * @param {string} planId
 * @param {string} digest
 * @returns {Promise<Object>}
 */
export function approveAutomationPlan(planId, digest) {
	return request(`${BASE}/automation/plans/${encodeURIComponent(planId)}/approve`, {}, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ digest })
	});
}

/**
 * Cancel a plan before it starts applying.
 * @param {string} planId
 * @returns {Promise<Object>}
 */
export function cancelAutomationPlan(planId) {
	return request(`${BASE}/automation/plans/${encodeURIComponent(planId)}/cancel`, {}, {
		method: 'POST'
	});
}

/**
 * Discard an unapproved (DRAFT/VALIDATED) preview so that previewing the same
 * workload again does not hit WORKLOAD_PLAN_ALREADY_EXISTS (D6, BUG-48).
 *
 * `DELETE /automation/plans/{id}` answers 204 and marks the plan SUPERSEDED;
 * any other plan status answers 409. There is deliberately no fallback to
 * POST .../cancel: that endpoint also cancels APPROVED plans. A server
 * without the route rejects with 404/405 and the preview simply expires.
 * @param {string} planId
 * @returns {Promise<{discarded: boolean, status: number}>}
 */
export async function discardAutomationPlan(planId) {
	const res = await send(`${BASE}/automation/plans/${encodeURIComponent(planId)}`, {}, { method: 'DELETE' });
	return { discarded: true, status: res.status };
}

/**
 * Create or retrieve the persisted rollback preview for a synced plan.
 * @param {string} planId
 * @returns {Promise<Object>}
 */
export function previewAutomationPlanRollback(planId) {
	return request(`${BASE}/automation/plans/${encodeURIComponent(planId)}/rollback-preview`, {}, {
		method: 'POST'
	});
}

/**
 * @param {number} id
 * @returns {Promise<Object[]>}
 */
export function getRecommendationPullRequests(id) {
	return request(`${BASE}/recommendations/${id}/pull-requests`);
}

/** Pending or open pull requests (cluster-wide: the API has no namespace filter). */
export function getOpenPullRequests({ signal } = {}) {
	return request(`${BASE}/automation/pull-requests`, {}, { signal });
}

/**
 * @param {number} id
 * @returns {Promise<Object[]>}
 */
export function getRecommendationEvents(id) {
	return request(`${BASE}/recommendations/${id}/events`);
}

/** @returns {Promise<Object>} */
export function getAutomationStatus({ signal } = {}) {
	return request(`${BASE}/automation/status`, {}, { signal });
}

/** @returns {Promise<Object>} */
export function getGitOpsStatus() {
	return request(`${BASE}/gitops/status`);
}

/** @param {{cluster?: string}} [params] */
export function refreshGitOpsDiscovery({ cluster } = {}) {
	return request(
		`${BASE}/gitops/refresh`,
		{ cluster },
		{ method: 'POST' }
	);
}

/** @returns {Promise<Object>} */
export function getGitOpsCoverage() {
	return request(`${BASE}/gitops/coverage`);
}

/** @param {{cluster?: string, status?: string[], limit?: number}} [params] */
export function getGitOpsBindings({
	cluster,
	status = ['ambiguous', 'unresolved', 'blocked', 'stale'],
	limit = 100
} = {}) {
	return request(`${BASE}/gitops/bindings`, { cluster, status, limit });
}

/**
 * Resolve a workload using only a candidate and generation returned by GitOps discovery.
 * @param {{cluster?: string, namespace: string, workload_kind: string, workload_name: string,
 *   candidate_id: string, generation_token: string, confirmed: boolean}} resolution
 */
export function resolveGitOpsBinding(resolution) {
	const {
		cluster,
		namespace,
		workload_kind,
		workload_name,
		candidate_id,
		generation_token,
		confirmed
	} = resolution;
	const path = `${BASE}/gitops/bindings/${encodeURIComponent(namespace)}/${encodeURIComponent(workload_kind)}/${encodeURIComponent(workload_name)}/resolve`;
	return request(
		path,
		{ cluster },
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ candidate_id, generation_token, confirmed })
		}
	);
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {string} [opts.start]
 * @param {string} [opts.end]
 * @param {number[]} [opts.years]
 * @param {boolean} [opts.aggregate]
 * @param {string} [opts.granularity]
 * @param {string} [opts.group_by]
 * @returns {Promise<Object>}
 */
export function getReportSummary({ namespace, last, start, end, years, aggregate, granularity, group_by, signal } = {}) {
	return request(`${BASE}/report/summary`, {
		namespace,
		last,
		start,
		end,
		years,
		aggregate: aggregate || undefined,
		granularity,
		group_by
	}, { signal });
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @returns {Promise<number[]>}
 */
export function getReportYears({ namespace } = {}) {
	return request(`${BASE}/report/years`, { namespace });
}

/**
 * Build the URL for report export (used for direct browser download).
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {string} [opts.start]
 * @param {string} [opts.end]
 * @param {number[]} [opts.years]
 * @param {boolean} [opts.aggregate]
 * @param {string} [opts.granularity]
 * @param {string} [opts.group_by]
 * @param {string} [opts.format]
 * @returns {string}
 */
export function buildReportExportUrl({ namespace, last, start, end, years, aggregate, granularity, group_by, format } = {}) {
	const url = new URL(`${BASE}/report/export`, window.location.origin);
	addSearchParam(url, 'namespace', namespace);
	addSearchParam(url, 'last', last);
	addSearchParam(url, 'start', start);
	addSearchParam(url, 'end', end);
	addSearchParam(url, 'years', years);
	addSearchParam(url, 'aggregate', aggregate ? 'true' : undefined);
	addSearchParam(url, 'granularity', granularity);
	addSearchParam(url, 'group_by', group_by);
	addSearchParam(url, 'format', format);
	return url.toString();
}

/**
 * Fetch the pre-computed dashboard KPI summary rows.
 * Returns a map of window slug → summary row for the fastest possible
 * dashboard load.
 *
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @returns {Promise<{windows: Object, namespace: string|null}>}
 */
export function getDashboardSummary({ namespace, signal } = {}) {
	return request(`${BASE}/metrics/dashboard-summary`, { namespace }, { signal });
}

/**
 * Fetch the pre-computed time-series chart data for a specific window.
 * Returns an ordered array of buckets ready for the chart builders.
 *
 * @param {Object} opts
 * @param {string} opts.windowSlug  — '24h' | '7d' | '30d' | '1y' | 'ytd'
 * @param {string} [opts.namespace]
 * @returns {Promise<{window_slug: string, namespace: string|null, points: Object[]}>}
 */
export function getDashboardTimeseries({ windowSlug, namespace, signal } = {}) {
	return request(`${BASE}/metrics/dashboard-timeseries/${windowSlug}`, { namespace }, { signal });
}

/**
 * Trigger an on-demand refresh of the pre-computed dashboard summary and
 * timeseries cache.  The backend responds immediately (HTTP 202) and runs
 * the refresh in the background.
 *
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @returns {Promise<{detail: string}>}
 */
export function refreshDashboardSummary({ namespace } = {}) {
	return request(`${BASE}/metrics/dashboard-summary/refresh`, { namespace }, { method: 'POST' });
}

/** Download a report with credentials/headers that anchor navigation cannot send. */
export async function downloadReport(params = {}) {
	const res = await send(`${BASE}/report/export`, params);
	const blob = await res.blob();
	const disposition = res.headers.get('Content-Disposition') || '';
	const filename = disposition.match(/filename="?([^"]+)"?/)?.[1]
		|| `greenkube-report.${params.format || 'csv'}`;
	const objectUrl = URL.createObjectURL(blob);
	try {
		const anchor = document.createElement('a');
		anchor.href = objectUrl;
		anchor.download = filename;
		document.body.appendChild(anchor);
		anchor.click();
		anchor.remove();
	} finally {
		URL.revokeObjectURL(objectUrl);
	}
}
