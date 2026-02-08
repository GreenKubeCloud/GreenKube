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
export function getMetrics({ namespace, last } = {}) {
	return request(`${BASE}/metrics`, { namespace, last });
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
