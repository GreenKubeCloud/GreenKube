/**
 * Savings-first Overview (K1).
 *
 * Principle 3: projected ≠ allocated ≠ realized. The Overview shows three
 * kinds of saving side by side and never adds them together:
 *
 * - realized: measured savings from the ledger (`realized_to_date`, accrued,
 *   and the measured annual `run_rate_annual`);
 * - pending verification: the projected annual savings of applied changes
 *   that have not been measured yet (`projected_annual`);
 * - opportunity: the projected annual savings of active recommendations that
 *   have not been applied, plus their risk-adjusted expected value.
 *
 * None of the returned objects carries a total across kinds, by design.
 */
import { eligibilityOf, expectedValue } from '$lib/utils/decision.js';

function finite(value) {
	const number = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
	return typeof number === 'number' && Number.isFinite(number) ? number : null;
}

function block(raw) {
	if (!raw || typeof raw !== 'object') return null;
	const cost = finite(raw.cost);
	const co2e = finite(raw.co2e_grams);
	if (cost === null && co2e === null) return null;
	return {
		cost: cost ?? 0,
		co2eGrams: co2e ?? 0,
		count: finite(raw.recommendation_count) ?? 0,
		method: typeof raw.method === 'string' ? raw.method : null
	};
}

/** The pricing currency of the recommendations (all share one), USD by default. */
export function currencyOf(recommendations = []) {
	for (const rec of recommendations) {
		const currency = rec?.value?.currency ?? rec?.impact?.currency;
		if (typeof currency === 'string' && /^[A-Z]{3}$/.test(currency)) return currency;
	}
	return 'USD';
}

/**
 * Projected opportunity of active recommendations (savings lane only).
 * Reliability recommendations cost money on purpose and are counted apart.
 */
export function opportunityOf(recommendations = []) {
	let cost = 0;
	let co2eGrams = 0;
	let ev = 0;
	let count = 0;
	let reliabilityCount = 0;
	let hasEv = false;
	for (const rec of Array.isArray(recommendations) ? recommendations : []) {
		if (rec?.value?.lane === 'reliability') {
			reliabilityCount += 1;
			continue;
		}
		count += 1;
		const recCost = finite(rec?.potential_savings_cost);
		const recCo2e = finite(rec?.potential_savings_co2e_grams);
		if (recCost !== null && recCost > 0) cost += recCost;
		if (recCo2e !== null && recCo2e > 0) co2eGrams += recCo2e;
		const recEv = expectedValue(rec);
		if (recEv !== null) {
			hasEv = true;
			if (recEv > 0) ev += recEv;
		}
	}
	return { cost, co2eGrams, ev: hasEv ? ev : null, count, reliabilityCount };
}

/**
 * Build the three never-summed savings panels from `GET /recommendations/savings`
 * and the active recommendations.
 */
export function buildSavingsOverview(savings, recommendations = []) {
	const realized = block(savings?.realized_to_date);
	const runRate = block(savings?.run_rate_annual);
	const pending = block(savings?.projected_annual);
	return {
		currency: currencyOf(recommendations),
		realized: {
			toDate: realized,
			runRateAnnual: runRate,
			verifiedCount: finite(savings?.verified_count) ?? 0,
			hasMeasurement: Boolean((realized && (realized.cost || realized.co2eGrams)) || (runRate && (runRate.cost || runRate.co2eGrams)))
		},
		pending: {
			annual: pending,
			appliedCount: finite(savings?.applied_count) ?? 0
		},
		opportunity: opportunityOf(recommendations),
		diagnostics: Array.isArray(savings?.diagnostics) ? savings.diagnostics.filter((item) => typeof item === 'string') : []
	};
}

/**
 * Warm-up state of the inbox: when no recommendation can open a PR yet and
 * some are waiting for evidence, the earliest `eligible_at`.
 * @returns {{waiting: number, eligibleAt: string|null} | null}
 */
export function warmUpOf(recommendations = [], now = Date.now()) {
	let waiting = 0;
	let earliest = null;
	let ready = 0;
	for (const rec of recommendations) {
		const eligibility = eligibilityOf(rec);
		if (eligibility?.canOpenPr) ready += 1;
		if (!eligibility?.eligibleAt) continue;
		const at = Date.parse(eligibility.eligibleAt);
		if (!Number.isFinite(at) || at <= now) continue;
		waiting += 1;
		if (earliest === null || at < earliest) earliest = at;
	}
	if (!waiting || ready) return null;
	return { waiting, eligibleAt: new Date(earliest).toISOString() };
}

/** "~31h" / "~3 days" until an ISO time, or null when it already passed. */
export function etaFrom(iso, now = Date.now()) {
	const at = Date.parse(iso);
	if (!Number.isFinite(at)) return null;
	const hours = (at - now) / 3_600_000;
	if (hours <= 0) return null;
	if (hours < 1) return `~${Math.max(1, Math.round(hours * 60))} min`;
	if (hours < 48) return `~${Math.round(hours)}h`;
	return `~${Math.round(hours / 24)} days`;
}

/** Display name of a recommendation's target. */
export function targetName(rec) {
	if (rec?.owner_name) return rec.owner_kind ? `${rec.owner_kind}/${rec.owner_name}` : rec.owner_name;
	return rec?.pod_name ?? rec?.target_node ?? rec?.namespace ?? 'Cluster-wide';
}

/** One-line delivery state of a recommendation from the server's eligibility. */
export function deliveryHint(rec) {
	const eligibility = eligibilityOf(rec);
	if (!eligibility) return null;
	if (eligibility.canOpenPr) return { ready: true, text: 'Ready for a pull request' };
	const blocker = eligibility.blockers[0];
	if (blocker) return { ready: false, text: blocker.message || blocker.code };
	return { ready: false, text: 'Not deliverable as a pull request' };
}
