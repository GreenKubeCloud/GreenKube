/**
 * GreenKube API client.
 * 
 * The API base URL is resolved at runtime:
 * - In development, Vite proxies /api to localhost:8000
 * - In production (K8s), NGINX proxies /api to the greenkube-api service
 */

const BASE = '/api/v1';
let apiToken = '';

/** Set a request-scoped bearer token without persisting it in browser storage. */
export function setApiToken(token) {
	apiToken = typeof token === 'string' ? token : '';
}

export function clearApiToken() {
	apiToken = '';
}

export class ApiError extends Error {
	constructor(message, { status, detail, action, retryable = false, retryAfter } = {}) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
		this.action = action;
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

function requestOptions(options = {}) {
	const headers = { ...(options.headers || {}) };
	if (apiToken) headers.Authorization = `Bearer ${apiToken}`;
	return { ...options, headers, credentials: 'include' };
}

function errorDetails(body, status) {
	const detail = typeof body?.detail === 'string'
		? body.detail
		: body?.detail?.message || body?.message;
	return {
		message: detail || `API error ${status}`,
		detail,
		action: body?.action || body?.detail?.action,
		retryable: body?.retryable === true || status === 408 || status === 429 || status >= 500,
		retryAfter: body?.retry_after || body?.detail?.retry_after
	};
}

async function request(path, params = {}, options = {}) {
	const url = new URL(path, window.location.origin);
	Object.entries(params).forEach(([k, v]) => {
		addSearchParam(url, k, v);
	});

	const res = await fetch(url.toString(), requestOptions(options));
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		const details = errorDetails(body, res.status);
		if (res.status === 401 && typeof window !== 'undefined') {
			window.dispatchEvent(new CustomEvent('greenkube-auth-required'));
		}
		throw new ApiError(details.message, { status: res.status, ...details });
	}
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
	const url = new URL(`${BASE}/config/services`, window.location.origin);
	const res = await fetch(url.toString(), requestOptions({
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(config)
	}));
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		const details = errorDetails(body, res.status);
		throw new ApiError(details.message, { status: res.status, ...details });
	}
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

/** @returns {Promise<Object[]>} */
export function getActiveRecommendations({ namespace, refresh } = {}) {
	return request(`${BASE}/recommendations/active`, { namespace, refresh });
}

/**
 * Ranked active recommendations.
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {number} [opts.limit]
 * @param {'co2'|'cost'} [opts.metric]
 * @param {string} [opts.profile] — balanced | carbon_first | cost_first | quick_wins | low_risk
 * @returns {Promise<Object[]>}
 */
export function getTopRecommendations({ namespace, limit, metric, profile, refresh } = {}) {
	return request(`${BASE}/recommendations/top`, { namespace, limit, metric, profile, refresh });
}

/**
 * Full recommendation detail, including the evidence block.
 * @param {number} id
 * @returns {Promise<Object>}
 */
export function getRecommendation(id) {
	return request(`${BASE}/recommendations/${id}`);
}

/** @returns {Promise<Object[]>} */
export function getIgnoredRecommendations() {
	return request(`${BASE}/recommendations/ignored`);
}

/** @returns {Promise<Object[]>} */
export function getAppliedRecommendations() {
	return request(`${BASE}/recommendations/applied`);
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @returns {Promise<Object>}
 */
export function getRecommendationSavings({ namespace, last } = {}) {
	return request(`${BASE}/recommendations/savings`, { namespace, last });
}

/**
 * @param {number} id
 * @param {{ carbon_saved_co2e_grams?: number, cost_saved?: number }} [body]
 * @returns {Promise<Object>}
 */
export async function applyRecommendation(id, body = {}) {
	const url = new URL(`${BASE}/recommendations/${id}/apply`, window.location.origin);
	const res = await fetch(url.toString(), requestOptions({
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	}));
	if (!res.ok) {
		const b = await res.json().catch(() => ({}));
		const details = errorDetails(b, res.status);
		throw new ApiError(details.message, { status: res.status, ...details });
	}
	return res.json();
}

/**
 * @param {number} id
 * @param {{ reason: ?string }} body
 * @returns {Promise<Object>}
 */
export async function ignoreRecommendation(id, body) {
	const url = new URL(`${BASE}/recommendations/${id}/ignore`, window.location.origin);
	const res = await fetch(url.toString(), requestOptions({
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	}));
	if (!res.ok) {
		const b = await res.json().catch(() => ({}));
		const details = errorDetails(b, res.status);
		throw new ApiError(details.message, { status: res.status, ...details });
	}
	return res.json();
}

/**
 * @param {number} id
 * @returns {Promise<Object>}
 */
export async function unignoreRecommendation(id) {
	const url = new URL(`${BASE}/recommendations/${id}/ignore`, window.location.origin);
	const res = await fetch(url.toString(), requestOptions({ method: 'DELETE' }));
	if (!res.ok) {
		const b = await res.json().catch(() => ({}));
		const details = errorDetails(b, res.status);
		throw new ApiError(details.message, { status: res.status, ...details });
	}
	return res.json();
}

/**
 * Preview or open a pull request that applies a rightsizing recommendation.
 * @param {number} id
 * @param {{ dry_run?: boolean, base_branch?: string }} [body]
 * @returns {Promise<Object>}
 */
export async function applyRecommendationPr(id, body = {}) {
	const url = new URL(`${BASE}/recommendations/${id}/apply-pr`, window.location.origin);
	const res = await fetch(url.toString(), requestOptions({
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	}));
	if (!res.ok) {
		const b = await res.json().catch(() => ({}));
		const details = errorDetails(b, res.status);
		throw new ApiError(details.message, { status: res.status, ...details });
	}
	return res.json();
}

/**
 * @param {number} id
 * @returns {Promise<Object[]>}
 */
export function getRecommendationPullRequests(id) {
	return request(`${BASE}/recommendations/${id}/pull-requests`);
}

export function getRecommendationPrEligibility(id) {
	return request(`${BASE}/recommendations/${id}/apply-pr/eligibility`);
}

export function getOpenPullRequests() {
	return request(`${BASE}/automation/pull-requests`);
}

/**
 * @param {number} id
 * @returns {Promise<Object[]>}
 */
export function getRecommendationEvents(id) {
	return request(`${BASE}/recommendations/${id}/events`);
}

/** @returns {Promise<Object>} */
export function getAutomationStatus() {
	return request(`${BASE}/automation/status`);
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
export async function refreshDashboardSummary({ namespace } = {}) {
	const url = new URL(`${BASE}/metrics/dashboard-summary/refresh`, window.location.origin);
	if (namespace) url.searchParams.set('namespace', namespace);
	const res = await fetch(url.toString(), requestOptions({ method: 'POST' }));
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		const details = errorDetails(body, res.status);
		throw new ApiError(details.message, { status: res.status, ...details });
	}
	return res.json();
}

/** Download a report with credentials/headers that anchor navigation cannot send. */
export async function downloadReport(params = {}) {
	const url = new URL(`${BASE}/report/export`, window.location.origin);
	Object.entries(params).forEach(([key, value]) => addSearchParam(url, key, value));
	const res = await fetch(url.toString(), requestOptions());
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new Error(body.detail || `API error ${res.status}`);
	}
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

/** Return the durable status of an asynchronous automation operation. */
export function getAutomationOperation(operationId, { signal } = {}) {
	return request(`${BASE}/automation/operations/${encodeURIComponent(operationId)}`, {}, { signal });
}

/**
 * Poll an asynchronous operation until it reaches a terminal state.
 * The operation endpoint is authoritative; no browser secret or operation
 * payload is retained between attempts.
 */
export async function waitForAutomationOperation(
	operationId,
	{ intervalMs = 1000, maxAttempts = 30, onUpdate, signal } = {}
) {
	let last;
	for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
		last = await getAutomationOperation(operationId, { signal });
		onUpdate?.(last);
		if (['completed', 'succeeded', 'failed', 'error', 'cancelled'].includes(last?.status)) return last;
		if (attempt < maxAttempts - 1) {
			await new Promise((resolve, reject) => {
				const timer = setTimeout(resolve, intervalMs);
				signal?.addEventListener('abort', () => {
					clearTimeout(timer);
					reject(signal.reason || new DOMException('Aborted', 'AbortError'));
				}, { once: true });
			});
		}
	}
	throw new ApiError('The operation is still in progress. Try again to check its status.', {
		retryable: true,
		action: 'Check the operation status again.'
	});
}
