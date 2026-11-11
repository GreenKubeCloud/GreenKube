/**
 * Applied changes and their measurements (`/api/v2/changes`, C2) for the
 * change and ledger pages that PR deep links open (J1):
 * `/changes/{plan_id}` and `/ledger?change={change_id or plan_id}`.
 */
import { ApiError, getChange, listChanges } from '$lib/api.js';

/** Measurement states with a label; status words are always shown as text. */
export const CHANGE_STATES = {
	applied: { label: 'Applied, waiting for measurement', tone: 'blue' },
	measuring: { label: 'Measuring', tone: 'blue' },
	realized: { label: 'Realized', tone: 'green' },
	not_realized: { label: 'Not realized', tone: 'yellow' },
	inconclusive: { label: 'Inconclusive', tone: 'dark' },
	contaminated: { label: 'Contaminated', tone: 'yellow' },
	regressed: { label: 'Regressed (rollback review)', tone: 'red' },
	closed: { label: 'Closed', tone: 'dark' }
};

export function stateLabel(state) {
	return CHANGE_STATES[state]?.label ?? (state ? String(state).replaceAll('_', ' ') : 'Not tracked yet');
}

export function stateTone(state) {
	return CHANGE_STATES[state]?.tone ?? 'dark';
}

/** Measured quantities with display names; `claimed` ones count as savings. */
export const QUANTITIES = {
	allocated_cost: { label: 'Allocated cost', kind: 'cost' },
	cost_cpu: { label: 'CPU cost', kind: 'cost' },
	cost_ram: { label: 'RAM cost', kind: 'cost' },
	co2e_g: { label: 'Operational CO₂e', kind: 'co2e' },
	core_hours: { label: 'Requested CPU', kind: 'unit' },
	gib_hours: { label: 'Requested memory', kind: 'unit' }
};

export function quantityLabel(quantity) {
	return QUANTITIES[quantity]?.label ?? String(quantity ?? '').replaceAll('_', ' ');
}

function isNotFound(error) {
	return error instanceof ApiError && (error.status === 404 || error.status === 422);
}

/**
 * Find the change a deep link points at: a change id, or a plan id whose
 * change the server filters (`GET /changes?plan_id=`, one request, J1).
 * @returns {Promise<Object|null>} the full change (`GET /changes/{id}`), or null
 */
export async function resolveChange(id, { signal, preferPlan = false } = {}) {
	if (!id) return null;
	const direct = async () => {
		try {
			return await getChange(id, { signal });
		} catch (error) {
			if (isNotFound(error)) return null;
			throw error;
		}
	};
	if (!preferPlan) {
		const change = await direct();
		if (change) return change;
	}
	let items = [];
	try {
		const result = await listChanges({ planId: id, limit: 1, signal });
		items = Array.isArray(result?.items) ? result.items : [];
	} catch (error) {
		// An id that cannot be a plan id is rejected with 422: it is not a plan.
		if (!isNotFound(error)) throw error;
	}
	// Never trust a server that ignored the filter: the change must name this plan.
	const match = items.find((item) => item?.plan_id === id);
	if (match) return getChange(match.change_id, { signal });
	return preferPlan ? direct() : null;
}

function finite(value) {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Measured estimates of one measurement, claimed (cost, CO₂e) first. */
export function measurementRows(measurement) {
	const estimates = Array.isArray(measurement?.estimates) ? measurement.estimates : [];
	return estimates
		.map((estimate) => ({
			quantity: estimate.quantity,
			label: quantityLabel(estimate.quantity),
			kind: QUANTITIES[estimate.quantity]?.kind ?? 'unit',
			unit: estimate.unit ?? '',
			claimed: estimate.claimed === true,
			annualized: finite(estimate.annualized),
			ciLow: finite(estimate.annualized_ci_low),
			ciHigh: finite(estimate.annualized_ci_high),
			projectedAnnual: finite(estimate.projected_annual),
			outcome: estimate.outcome ?? null,
			reason: estimate.reason ?? ''
		}))
		.sort((a, b) => Number(b.claimed) - Number(a.claimed));
}

/** The latest (current) measurement: the last one that nothing supersedes. */
export function latestMeasurement(items = []) {
	if (!Array.isArray(items) || !items.length) return null;
	const superseded = new Set(items.map((item) => item?.supersedes_measurement_id).filter(Boolean));
	const current = items.filter((item) => !superseded.has(item?.measurement_id));
	return (current.length ? current : items).at(-1);
}

/** "Deployment payments/api" for a change target. */
export function changeTarget(change) {
	const target = change?.target;
	if (!target) return 'Unknown workload';
	return `${target.kind ?? 'Workload'} ${target.namespace ?? ''}/${target.name ?? ''}`;
}
