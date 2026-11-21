/**
 * Savings-first Overview (K1).
 *
 * Principle 3: projected ≠ allocated ≠ realized. The Overview shows three
 * kinds of saving side by side and never adds them together:
 *
 * - measured allocated (`realized` key, kept for compatibility): what the
 *   changed workloads stop being charged, measured and accrued in the ledger
 *   (`realized_to_date`), and its annual run rate (`run_rate_annual`), with
 *   the realization factor that relates it to the bill. "Realized" in the UI
 *   is reserved for billed savings (`realized_billed`, when the server has it);
 * - pending verification: the projected annual savings of applied changes
 *   that have not been measured yet (`projected_annual`);
 * - opportunity: the projected annual savings of active recommendations that
 *   have not been applied, plus their risk-adjusted expected value.
 *
 * None of the returned objects carries a total across kinds, by design.
 */
import { eligibilityOf, expectedValue } from '$lib/utils/decision.js';
import { getDisplayCurrency } from '$lib/utils/format.js';
import { savingsFigures, visibleDiagnostics } from '$lib/savingsVocabulary.js';

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

/** The pricing currency of the recommendations (all share one), else the configured pricing currency. */
export function currencyOf(recommendations = []) {
	for (const rec of recommendations) {
		const currency = rec?.value?.currency ?? rec?.impact?.currency;
		if (typeof currency === 'string' && /^[A-Z]{3}$/.test(currency)) return currency;
	}
	return getDisplayCurrency();
}

/** Label of the de-duplicated opportunity (IMP-07), shown on every page that renders it. */
export const OPPORTUNITY_LABEL = 'Projected (not additive across overlapping recommendations)';

/**
 * The de-duplicated opportunity the server computed once (`opportunity` of
 * `GET /recommendations/savings`, IMP-07), or null when the server sent none.
 * Overlapping recommendations (a namespace deletion and the rightsizing of its
 * workloads, an off-peak schedule and a rightsizing of one workload, a node
 * removal and its pods) are counted once and request increases apart, so the
 * dashboard and the recommendations page show the same figure.
 */
export function serverOpportunity(savings) {
	const value = savings?.opportunity;
	if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
	const cost = finite(value.cost_annual);
	if (cost === null) return null;
	return {
		cost,
		allocatedCost: finite(value.allocated_cost_annual),
		co2eGrams: finite(value.co2e_grams_annual) ?? 0,
		reliabilityCost: finite(value.reliability_cost_annual) ?? 0,
		reliabilityCount: finite(value.reliability_count) ?? 0,
		count: finite(value.recommendation_count) ?? 0,
		label: typeof value.label === 'string' ? value.label : OPPORTUNITY_LABEL
	};
}

/**
 * Projected opportunity of active recommendations (savings lane only).
 * Reliability recommendations cost money on purpose and are counted apart.
 * With the server's de-duplicated opportunity (`server`, from
 * {@link serverOpportunity}) its totals replace the client-side sums, which
 * would add overlapping recommendations.
 */
export function opportunityOf(recommendations = [], server = null) {
	let cost = 0;
	let co2eGrams = 0;
	let ev = 0;
	let count = 0;
	let reliabilityCount = 0;
	let allocatedOnly = 0;
	let hasEv = false;
	for (const rec of Array.isArray(recommendations) ? recommendations : []) {
		if (rec?.value?.lane === 'reliability') {
			reliabilityCount += 1;
			continue;
		}
		count += 1;
		// An unknown billable saving (cluster not observed yet: null) is never summed as a projected saving.
		const billableUnknown = rec?.projected_billable_cost_annual === null;
		const recCost = billableUnknown ? null : finite(rec?.potential_savings_cost);
		const recCo2e = finite(rec?.potential_savings_co2e_grams);
		if (recCost !== null && recCost > 0) cost += recCost;
		if (recCo2e !== null && recCo2e > 0) co2eGrams += recCo2e;
		const allocated = finite(rec?.projected_allocated_cost_annual);
		if (allocated !== null && allocated > 0 && (billableUnknown || !(recCost > 0))) allocatedOnly += 1;
		const recEv = expectedValue(rec);
		if (recEv !== null) {
			hasEv = true;
			// The allocated (chargeback, not billed) value ranks fixed-node changes; it is not a saving.
			const allocatedValue = finite(rec?.value?.factors?.allocated_value ?? rec?.ranking_factors?.allocated_value) ?? 0;
			const savingEv = recEv - allocatedValue;
			if (savingEv > 0) ev += savingEv;
		}
	}
	if (server) {
		return {
			cost: server.cost,
			co2eGrams: server.co2eGrams,
			ev: hasEv ? ev : null,
			count: server.count,
			reliabilityCount: server.reliabilityCount,
			reliabilityCost: server.reliabilityCost,
			allocatedOnly,
			label: server.label
		};
	}
	return { cost, co2eGrams, ev: hasEv ? ev : null, count, reliabilityCount, allocatedOnly };
}

/**
 * Build the three never-summed savings panels from `GET /recommendations/savings`
 * and the active recommendations.
 */
export function buildSavingsOverview(savings, recommendations = []) {
	const figures = savingsFigures(savings);
	const realized = block(figures.measuredAllocatedToDate);
	const runRate = block(figures.measuredAllocatedAnnual);
	const pending = block(figures.projectedAnnual);
	const billed = block(figures.realizedBilledToDate);
	return {
		currency: currencyOf(recommendations),
		realized: {
			toDate: realized,
			runRateAnnual: runRate,
			verifiedCount: finite(savings?.verified_count) ?? 0,
			realizationFactor: figures.realizationFactor,
			expectedBilledCostAnnual: figures.expectedBilledCostAnnual,
			realizedBilledToDate: billed,
			hasMeasurement: Boolean((realized && (realized.cost || realized.co2eGrams)) || (runRate && (runRate.cost || runRate.co2eGrams)))
		},
		pending: {
			annual: pending,
			appliedCount: finite(savings?.applied_count) ?? 0
		},
		opportunity: opportunityOf(recommendations, serverOpportunity(savings)),
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
export function deliveryHint(rec, demo = false) {
	const eligibility = eligibilityOf(rec);
	if (!eligibility) return null;
	if (eligibility.canOpenPr) return { ready: true, text: 'Ready for a pull request' };
	// Demo mode: "no automation on this install" is said once on the page, not on every row.
	const blocker = visibleDiagnostics(eligibility, demo).blockers[0];
	if (!blocker && demo) return null;
	if (blocker) return { ready: false, text: blocker.message || blocker.code };
	return { ready: false, text: 'Not deliverable as a pull request' };
}
