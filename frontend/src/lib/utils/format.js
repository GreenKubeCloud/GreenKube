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

const CURRENCY_CODE = /^[A-Z]{3}$/;
let displayCurrency = 'USD';

/**
 * Set the pricing currency every amount is shown in (`GET /config` `currency`,
 * i.e. `impact.pricing.currency`). An invalid or missing code keeps the current one.
 * @param {unknown} code ISO 4217 code such as "EUR"
 * @returns {string} the currency now in use
 */
export function setDisplayCurrency(code) {
	const upper = typeof code === 'string' ? code.trim().toUpperCase() : '';
	if (CURRENCY_CODE.test(upper)) displayCurrency = upper;
	return displayCurrency;
}

/** The pricing currency amounts are shown in (USD until the server says otherwise). */
export function getDisplayCurrency() {
	return displayCurrency;
}

/** A valid ISO 4217 code, or the display currency. */
export function currencyOrDefault(code) {
	return typeof code === 'string' && CURRENCY_CODE.test(code) ? code : displayCurrency;
}

/** The symbol of a currency in en-US ("$", "€", "£"), or its code. */
export function currencySymbol(currency) {
	const code = currencyOrDefault(currency);
	try {
		const part = new Intl.NumberFormat('en-US', { style: 'currency', currency: code })
			.formatToParts(0)
			.find((item) => item.type === 'currency');
		return part?.value || code;
	} catch {
		return code;
	}
}

/**
 * Format a cost in the pricing currency (never a hard-coded "$").
 * @param {number} cost
 * @param {string} [currency] ISO 4217 code; defaults to the display currency
 * @returns {string}
 */
export function formatCost(cost, currency) {
	if (cost == null || !Number.isFinite(cost)) return '—';
	const code = currencyOrDefault(currency);
	const magnitude = Math.abs(cost);
	const digits = magnitude >= 1 ? 2 : magnitude >= 0.01 ? 3 : 4;
	try {
		return new Intl.NumberFormat('en-US', {
			style: 'currency',
			currency: code,
			minimumFractionDigits: digits,
			maximumFractionDigits: digits,
			useGrouping: false
		}).format(cost);
	} catch {
		return `${cost.toFixed(digits)} ${code}`;
	}
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
