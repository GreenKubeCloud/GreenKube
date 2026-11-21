/**
 * Node idle share on the Footprint page (WP-05.3, NRG-02).
 *
 * The `total_*` fields of a summary are allocated to pods. Cluster-wide
 * summaries add the node idle share (`idle_*`: unreserved capacity,
 * `__idle__` rows) and the cluster totals (`cluster_total_*` = allocated +
 * idle). They are null with a namespace filter, without idle rows, or when
 * the window holds pod rows older than the first idle row
 * (`idle_data_since`): such values are unknown, never zero.
 */

export const NOT_AVAILABLE = 'not available';

/** A finite number, or null (null, undefined and non-numbers are unknown). */
function known(value) {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Sum of the values, or null as soon as one is unknown. */
function sumKnown(...values) {
	const numbers = values.map(known);
	return numbers.some((value) => value === null) ? null : numbers.reduce((a, b) => a + b, 0);
}

/**
 * Allocated, idle and cluster-total values of CO₂e (Scope 2 + 3), cost and energy.
 * @param {Record<string, any>|null|undefined} summary a `/metrics/summary` or dashboard-summary window
 * @returns {{key: string, label: string, allocated: number|null, idle: number|null, total: number|null, idleShare: number|null}[]}
 */
export function idleShareRows(summary) {
	const s = summary ?? {};
	const rows = [
		{
			key: 'co2e',
			label: 'CO₂e (Scope 2+3)',
			allocated: known(s.total_co2e_all_scopes) ?? sumKnown(s.total_co2e_grams, s.total_embodied_co2e_grams ?? 0),
			idle: sumKnown(s.idle_co2e_grams, s.idle_embodied_co2e_grams),
			total: sumKnown(s.cluster_total_co2e_grams, s.cluster_total_embodied_co2e_grams)
		},
		{ key: 'cost', label: 'Cost', allocated: known(s.total_cost), idle: known(s.idle_cost), total: known(s.cluster_total_cost) },
		{
			key: 'energy',
			label: 'Energy',
			allocated: known(s.total_energy_joules),
			idle: known(s.idle_energy_joules),
			total: known(s.cluster_total_energy_joules)
		}
	];
	return rows.map((row) => {
		// Idle and total are known together, or not at all.
		const idle = row.total === null ? null : row.idle;
		const total = row.idle === null ? null : row.total;
		return { ...row, idle, total, idleShare: idle !== null && total ? idle / total : null };
	});
}

/**
 * Why the idle share is not available ('' when it is).
 * @param {Record<string, any>|null|undefined} summary
 * @param {string|null|undefined} namespace the selected namespace filter
 */
export function idleShareNote(summary, namespace) {
	if (namespace) return 'The node idle share belongs to no namespace: shown for the whole cluster only.';
	const s = summary ?? {};
	if (known(s.idle_energy_joules) !== null && known(s.cluster_total_energy_joules) !== null) return '';
	if (s.idle_data_since) {
		const since = new Date(s.idle_data_since);
		const label = Number.isNaN(since.getTime()) ? String(s.idle_data_since) : since.toISOString().slice(0, 16).replace('T', ' ');
		return `The idle history starts ${label} UTC, after the start of this period: the idle share would cover part of it only.`;
	}
	return 'No node idle row is stored for this period yet.';
}
