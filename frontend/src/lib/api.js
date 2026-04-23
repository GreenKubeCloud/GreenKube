/**
 * GreenKube API client.
 * 
 * The API base URL is resolved at runtime:
 * - In development, Vite proxies /api to localhost:8000
 * - In production (K8s), NGINX proxies /api to the greenkube-api service
 */

const BASE = '/api/v1';

async function request(path, params = {}) {
	const url = new URL(path, window.location.origin);
	Object.entries(params).forEach(([k, v]) => {
		if (v !== null && v !== undefined && v !== '') {
			url.searchParams.set(k, v);
		}
	});

	const res = await fetch(url.toString());
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new Error(body.detail || `API error ${res.status}`);
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
 * @returns {Promise<Object>}
 */
export async function updateServiceConfig(config) {
	const url = new URL(`${BASE}/config/services`, window.location.origin);
	const res = await fetch(url.toString(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(config)
	});
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new Error(body.detail || `API error ${res.status}`);
	}
	return res.json();
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

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @returns {Promise<Object>}
 */
export function getMetricsSummary({ namespace, last } = {}) {
	return request(`${BASE}/metrics/summary`, { namespace, last });
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {string} [opts.granularity]
 * @returns {Promise<Object[]>}
 */
export function getTimeseries({ namespace, last, granularity } = {}) {
	return request(`${BASE}/metrics/timeseries`, { namespace, last, granularity });
}

/**
 * Lightweight SQL-level aggregation of metrics by namespace.
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @returns {Promise<Object[]>}
 */
export function getMetricsByNamespace({ namespace, last } = {}) {
	return request(`${BASE}/metrics/by-namespace`, { namespace, last });
}

/**
 * Lightweight SQL-level aggregation of top pods by CO₂.
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {number} [opts.limit]
 * @returns {Promise<Object[]>}
 */
export function getTopPods({ namespace, last, limit } = {}) {
	return request(`${BASE}/metrics/top-pods`, { namespace, last, limit });
}

/** @returns {Promise<Object[]>} */
export function getNodes() {
	return request(`${BASE}/nodes`);
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
export function getActiveRecommendations({ namespace } = {}) {
	return request(`${BASE}/recommendations/active`, { namespace });
}

/** @returns {Promise<Object[]>} */
export function getIgnoredRecommendations() {
	return request(`${BASE}/recommendations/ignored`);
}

/** @returns {Promise<Object[]>} */
export function getAppliedRecommendations() {
	return request(`${BASE}/recommendations/applied`);
}

/** @returns {Promise<Object>} */
export function getRecommendationSavings() {
	return request(`${BASE}/recommendations/savings`);
}

/**
 * @param {number} id
 * @param {{ carbon_saved_co2e_grams?: number, cost_saved?: number }} [body]
 * @returns {Promise<Object>}
 */
export async function applyRecommendation(id, body = {}) {
	const url = new URL(`${BASE}/recommendations/${id}/apply`, window.location.origin);
	const res = await fetch(url.toString(), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) {
		const b = await res.json().catch(() => ({}));
		throw new Error(b.detail || `API error ${res.status}`);
	}
	return res.json();
}

/**
 * @param {number} id
 * @param {{ reason: string }} body
 * @returns {Promise<Object>}
 */
export async function ignoreRecommendation(id, body) {
	const url = new URL(`${BASE}/recommendations/${id}/ignore`, window.location.origin);
	const res = await fetch(url.toString(), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) {
		const b = await res.json().catch(() => ({}));
		throw new Error(b.detail || `API error ${res.status}`);
	}
	return res.json();
}

/**
 * @param {number} id
 * @returns {Promise<Object>}
 */
export async function unignoreRecommendation(id) {
	const url = new URL(`${BASE}/recommendations/${id}/ignore`, window.location.origin);
	const res = await fetch(url.toString(), { method: 'DELETE' });
	if (!res.ok) {
		const b = await res.json().catch(() => ({}));
		throw new Error(b.detail || `API error ${res.status}`);
	}
	return res.json();
}

/**
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {boolean} [opts.aggregate]
 * @param {string} [opts.granularity]
 * @returns {Promise<Object>}
 */
export function getReportSummary({ namespace, last, aggregate, granularity } = {}) {
	return request(`${BASE}/report/summary`, { namespace, last, aggregate: aggregate || undefined, granularity });
}

/**
 * Build the URL for report export (used for direct browser download).
 * @param {Object} opts
 * @param {string} [opts.namespace]
 * @param {string} [opts.last]
 * @param {boolean} [opts.aggregate]
 * @param {string} [opts.granularity]
 * @param {string} [opts.format]
 * @returns {string}
 */
export function buildReportExportUrl({ namespace, last, aggregate, granularity, format } = {}) {
	const url = new URL(`${BASE}/report/export`, window.location.origin);
	if (namespace) url.searchParams.set('namespace', namespace);
	if (last) url.searchParams.set('last', last);
	if (aggregate) url.searchParams.set('aggregate', 'true');
	if (granularity) url.searchParams.set('granularity', granularity);
	if (format) url.searchParams.set('format', format);
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
export function getDashboardSummary({ namespace } = {}) {
	return request(`${BASE}/metrics/dashboard-summary`, { namespace });
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
export function getDashboardTimeseries({ windowSlug, namespace } = {}) {
	return request(`${BASE}/metrics/dashboard-timeseries/${windowSlug}`, { namespace });
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
	const res = await fetch(url.toString(), { method: 'POST' });
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new Error(body.detail || `API error ${res.status}`);
	}
	return res.json();
}
