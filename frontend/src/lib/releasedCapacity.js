/**
 * Released capacity (WP-08.1): the third quantity next to cost and CO2e.
 *
 * Reserved capacity is shown in its own units (cores, GiB, core-hours,
 * GiB-hours, LoadBalancers), always with its projected or measured label,
 * never converted to money and never added to a savings total. Negative
 * values are increases ("more reserved"), never shown as a saving.
 */

function finite(value) {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function nonZero(value) {
	const number = finite(value);
	return number !== null && Math.abs(number) > 1e-9 ? number : null;
}

/** Formats a signed capacity amount with its unit; increases carry a minus sign. */
export function formatCapacity(value, unit, digits = 2) {
	const number = finite(value);
	if (number === null) return '—';
	const sign = number < 0 ? '−' : '';
	const abs = Math.abs(number);
	const text = abs >= 100 ? abs.toLocaleString('en-US', { maximumFractionDigits: 0 }) : abs.toFixed(digits);
	return `${sign}${text} ${unit}`;
}

/**
 * One-line summary for list rows: "Frees 0.60 cores · 0.50 GiB (projected)",
 * "Reserves 1.00 GiB more (projected)" for an increase; null when empty.
 */
export function capacitySummary(capacity) {
	if (!capacity || typeof capacity !== 'object') return null;
	const method = capacity.method === 'measured' ? 'measured' : 'projected';
	const now = capacity.instantaneous ?? capacity.total ?? {};
	const parts = [
		[now.cpu_cores ?? now.cpu_cores_per_replica, 'cores'],
		[now.mem_gib ?? now.mem_gib_per_replica, 'GiB'],
		[now.storage_gib, 'GiB storage'],
		[now.load_balancers, 'LoadBalancers']
	].filter(([value]) => nonZero(value) !== null);
	if (!parts.length) return null;
	const increase = parts.every(([value]) => value < 0);
	const text = parts.map(([value, unit]) => formatCapacity(increase ? -value : value, unit)).join(' · ');
	return increase ? `Reserves ${text} more (${method})` : `Frees ${text} (${method})`;
}

/**
 * Display rows of a projected (`recommendation.released_capacity`) or
 * measured (`measurement.capacity`) released-capacity object; null when
 * there is nothing to show.
 */
export function releasedCapacityOf(capacity) {
	if (!capacity || typeof capacity !== 'object') return null;
	const method = capacity.method === 'measured' ? 'measured' : 'projected';
	const rows = [];
	const now = capacity.instantaneous ?? capacity.total ?? {};
	const annual = capacity.annual ?? {};
	const perReplica = capacity.per_replica ?? {};
	const add = (key, label, value, unit) => {
		const number = nonZero(value);
		if (number !== null) rows.push({ key, label, value: number, unit, text: formatCapacity(number, unit) });
	};
	add('cpu_cores', 'CPU requests', now.cpu_cores, 'cores');
	add('mem_gib', 'Memory requests', now.mem_gib, 'GiB');
	add('cpu_cores_per_replica', 'CPU per replica', now.cpu_cores_per_replica ?? perReplica.cpu_cores, 'cores');
	add('mem_gib_per_replica', 'Memory per replica', now.mem_gib_per_replica ?? perReplica.mem_gib, 'GiB');
	add('storage_gib', 'Storage', now.storage_gib, 'GiB');
	add('load_balancers', 'LoadBalancers', now.load_balancers, 'count');
	add('cpu_core_hours', 'CPU per year', annual.cpu_core_hours, 'core-hours');
	add('mem_gib_hours', 'Memory per year', annual.mem_gib_hours, 'GiB-hours');
	add('cpu_core_hours_measured', 'CPU over the post-window', capacity.cpu_core_hours, 'core-hours');
	add('mem_gib_hours_measured', 'Memory over the post-window', capacity.mem_gib_hours, 'GiB-hours');
	if (!rows.length && !capacity.verdict) return null;
	const direction = typeof capacity.direction === 'string' ? capacity.direction : null;
	return {
		method,
		label: method === 'measured' ? 'Measured' : 'Projected',
		direction,
		increase: direction === 'increase',
		verdict: typeof capacity.verdict === 'string' ? capacity.verdict : null,
		replicas: finite(now.replicas ?? capacity.replicas?.after),
		replicaChange: capacity.replica_change ?? null,
		period: method === 'measured' ? 'post-window' : capacity.period === 'year' ? 'per year' : null,
		note:
			typeof capacity.note === 'string'
				? capacity.note
				: 'Reserved capacity, not money: never added to cost savings. Negative values are increases.',
		rows
	};
}
