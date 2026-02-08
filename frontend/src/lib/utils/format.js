/**
 * Utility functions for formatting values in the UI.
 */

/**
 * Format CO2 grams to human-readable string.
 * @param {number} grams
 * @returns {string}
 */
export function formatCO2(grams) {
	if (grams == null) return '—';
	if (grams >= 1000) return `${(grams / 1000).toFixed(2)} kg`;
	if (grams >= 1) return `${grams.toFixed(2)} g`;
	return `${(grams * 1000).toFixed(1)} mg`;
}

/**
 * Format cost in USD.
 * @param {number} cost
 * @returns {string}
 */
export function formatCost(cost) {
	if (cost == null) return '—';
	if (cost >= 1) return `$${cost.toFixed(2)}`;
	if (cost >= 0.01) return `$${cost.toFixed(3)}`;
	return `$${cost.toFixed(4)}`;
}

/**
 * Format energy in Joules to human-readable string.
 * @param {number} joules
 * @returns {string}
 */
export function formatEnergy(joules) {
	if (joules == null) return '—';
	if (joules >= 3.6e6) return `${(joules / 3.6e6).toFixed(2)} kWh`;
	if (joules >= 1000) return `${(joules / 1000).toFixed(1)} kJ`;
	return `${joules.toFixed(0)} J`;
}

/**
 * Format bytes to human-readable string.
 * @param {number} bytes
 * @returns {string}
 */
export function formatBytes(bytes) {
	if (bytes == null) return '—';
	const units = ['B', 'KB', 'MB', 'GB', 'TB'];
	let i = 0;
	let val = bytes;
	while (val >= 1024 && i < units.length - 1) {
		val /= 1024;
		i++;
	}
	return `${val.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

/**
 * Format CPU millicores.
 * @param {number} millicores
 * @returns {string}
 */
export function formatCPU(millicores) {
	if (millicores == null) return '—';
	if (millicores >= 1000) return `${(millicores / 1000).toFixed(1)} cores`;
	return `${millicores}m`;
}

/**
 * Format a relative time string.
 * @param {string|Date} date
 * @returns {string}
 */
export function formatRelativeTime(date) {
	if (!date) return '—';
	const d = typeof date === 'string' ? new Date(date) : date;
	const now = new Date();
	const diff = (now.getTime() - d.getTime()) / 1000;

	if (diff < 60) return 'just now';
	if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
	if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
	return `${Math.floor(diff / 86400)}d ago`;
}

/**
 * Format a date for display.
 * @param {string|Date} date
 * @returns {string}
 */
export function formatDate(date) {
	if (!date) return '—';
	const d = typeof date === 'string' ? new Date(date) : date;
	return d.toLocaleString('en-US', {
		month: 'short',
		day: 'numeric',
		hour: '2-digit',
		minute: '2-digit',
		hour12: false
	});
}

/**
 * Format a number with compact notation.
 * @param {number} n
 * @returns {string}
 */
export function formatNumber(n) {
	if (n == null) return '—';
	if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
	if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
	return n.toFixed(0);
}
