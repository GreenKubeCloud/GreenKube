/**
 * Savings-first Overview mapping (K1): realized, pending verification and
 * opportunity are three separate figures, never added together.
 */
import { describe, expect, it } from 'vitest';
import {
	buildSavingsOverview,
	currencyOf,
	deliveryHint,
	etaFrom,
	opportunityOf,
	targetName,
	warmUpOf
} from '$lib/overview.js';

const HOUR = 3_600_000;

const savings = {
	realized_to_date: { method: 'measured', basis: 'accrued', cost: 120, co2e_grams: 4000, recommendation_count: 0 },
	run_rate_annual: { method: 'measured', basis: 'annual_rate', cost: 900, co2e_grams: 30000, recommendation_count: 2 },
	projected_annual: { method: 'projected', basis: 'annual_rate', cost: 500, co2e_grams: 15000, recommendation_count: 3 },
	projected_to_date: { method: 'projected', basis: 'accrued', cost: 40, co2e_grams: 1000 },
	user_declared_annual: { method: 'user_declared', basis: 'annual_rate', cost: 77, co2e_grams: 0 },
	applied_count: 5,
	verified_count: 2,
	diagnostics: ['Cost source: pricing catalog.', 42],
	// Deprecated aliases a 0.3 server still returns; never read.
	total_cost_saved: 999999
};

const recommendations = [
	{ id: 1, potential_savings_cost: 300, potential_savings_co2e_grams: 9000, value: { ev: 250, currency: 'EUR', lane: 'savings' } },
	{ id: 2, potential_savings_cost: 100, potential_savings_co2e_grams: 1000, value: { ev: -20, currency: 'EUR' } },
	{ id: 3, potential_savings_cost: -80, potential_savings_co2e_grams: -500, value: { ev: 10, lane: 'reliability' } },
	{ id: 4, potential_savings_cost: null, potential_savings_co2e_grams: 'NaN' }
];

describe('buildSavingsOverview', () => {
	it('keeps realized, pending verification and opportunity apart and never sums them', () => {
		const overview = buildSavingsOverview(savings, recommendations);

		expect(overview.realized.toDate).toMatchObject({ cost: 120, co2eGrams: 4000, method: 'measured' });
		expect(overview.realized.runRateAnnual).toMatchObject({ cost: 900, co2eGrams: 30000 });
		expect(overview.realized.verifiedCount).toBe(2);
		expect(overview.realized.hasMeasurement).toBe(true);
		expect(overview.pending.annual).toMatchObject({ cost: 500, co2eGrams: 15000, method: 'projected' });
		expect(overview.pending.appliedCount).toBe(5);
		expect(overview.opportunity).toMatchObject({ cost: 400, co2eGrams: 10000, count: 3, reliabilityCount: 1 });

		// No figure anywhere is a sum across kinds (120+500+400, 900+500, …), and the
		// deprecated flat alias is ignored.
		const numbers = JSON.stringify(overview).match(/-?\d+(\.\d+)?/g).map(Number);
		for (const forbidden of [1020, 1400, 1800, 620, 999999, 77 + 120]) {
			expect(numbers).not.toContain(forbidden);
		}
		expect(Object.keys(overview)).not.toContain('total');
		expect(JSON.stringify(overview)).not.toMatch(/total/i);
	});

	it('uses the recommendations currency and drops non-string diagnostics', () => {
		const overview = buildSavingsOverview(savings, recommendations);
		expect(overview.currency).toBe('EUR');
		expect(overview.diagnostics).toEqual(['Cost source: pricing catalog.']);
	});

	it('reports no measurement when the ledger is empty or unavailable', () => {
		const empty = buildSavingsOverview(
			{ realized_to_date: { cost: 0, co2e_grams: 0 }, run_rate_annual: { cost: 0, co2e_grams: 0 } },
			[]
		);
		expect(empty.realized.hasMeasurement).toBe(false);
		expect(empty.opportunity).toEqual({ cost: 0, co2eGrams: 0, ev: null, count: 0, reliabilityCount: 0, allocatedOnly: 0 });

		const missing = buildSavingsOverview(null, []);
		expect(missing.realized.toDate).toBeNull();
		expect(missing.pending.annual).toBeNull();
		expect(missing.currency).toBe('USD');
	});
});

describe('opportunityOf', () => {
	it('counts only positive projected savings of the savings lane and positive EV', () => {
		expect(opportunityOf(recommendations)).toEqual({
			cost: 400,
			co2eGrams: 10000,
			ev: 250,
			count: 3,
			reliabilityCount: 1,
			allocatedOnly: 0
		});
	});

	it('tolerates a non-array input', () => {
		expect(opportunityOf(null).count).toBe(0);
	});

	it('keeps the allocated (chargeback) value out of the EV and unknown billable savings out of the cost', () => {
		const result = opportunityOf([
			// Fixed nodes: billable 0, the EV holds 90 of allocated value.
			{
				potential_savings_cost: 0,
				projected_billable_cost_annual: 0,
				projected_allocated_cost_annual: 100,
				value: { ev: 80, p_success: 0.9, factors: { allocated_value: 90 } }
			},
			// Unobserved cluster: the billable saving is unknown, potential_savings_cost is the allocated value.
			{
				potential_savings_cost: 50,
				projected_billable_cost_annual: null,
				projected_allocated_cost_annual: 50,
				projection_note: 'billable saving unknown: cluster not observed yet',
				value: { ev: 40, p_success: 0.9 }
			}
		]);
		expect(result.ev).toBe(40);
		expect(result.cost).toBe(0);
		expect(result.allocatedOnly).toBe(2);
	});
});

describe('currencyOf', () => {
	it('falls back to the impact currency, then USD', () => {
		expect(currencyOf([{ impact: { currency: 'GBP' } }])).toBe('GBP');
		expect(currencyOf([{ value: { currency: 'euro' } }])).toBe('USD');
	});
});

describe('warm-up', () => {
	const now = Date.now();
	const inHours = (h) => new Date(now + h * HOUR).toISOString();

	it('returns the earliest eligible time when nothing can open a PR yet', () => {
		const recs = [
			{ eligibility: { can_open_pr: false, eligible_at: inHours(31), blockers: [] } },
			{ eligibility: { can_open_pr: false, eligible_at: inHours(5), blockers: [] } },
			{ eligibility: { can_open_pr: false, eligible_at: inHours(-2), blockers: [] } }
		];
		const warmUp = warmUpOf(recs, now);
		expect(warmUp.waiting).toBe(2);
		expect(Date.parse(warmUp.eligibleAt)).toBe(Date.parse(inHours(5)));
	});

	it('is null once a recommendation is ready', () => {
		const recs = [
			{ eligibility: { can_open_pr: true, blockers: [] } },
			{ eligibility: { can_open_pr: false, eligible_at: inHours(5), blockers: [] } }
		];
		expect(warmUpOf(recs, now)).toBeNull();
	});

	it('formats an ETA relative to an injected clock', () => {
		expect(etaFrom(inHours(31), now)).toBe('~31h');
		expect(etaFrom(inHours(72), now)).toBe('~3 days');
		expect(etaFrom(inHours(0.5), now)).toBe('~30 min');
		expect(etaFrom(inHours(-1), now)).toBeNull();
		expect(etaFrom('not a date', now)).toBeNull();
	});
});

describe('deliveryHint and targetName', () => {
	it('uses the server eligibility, first blocker first', () => {
		expect(deliveryHint({ eligibility: { can_open_pr: true } })).toEqual({ ready: true, text: 'Ready for a pull request' });
		expect(
			deliveryHint({
				eligibility: { can_open_pr: false, blockers: [{ code: 'binding_unresolved', message: 'No Git source', fix: 'Resolve it' }] }
			})
		).toEqual({ ready: false, text: 'No Git source' });
		expect(deliveryHint({})).toBeNull();
	});

	it('drops the "no automation" blockers in demo mode only', () => {
		const rec = {
			eligibility: {
				can_open_pr: false,
				blockers: [
					{ code: 'action_unavailable', message: 'No typed change' },
					{ code: 'automation_not_configured', message: 'No plan service' }
				]
			}
		};
		expect(deliveryHint(rec)).toEqual({ ready: false, text: 'No typed change' });
		expect(deliveryHint(rec, true)).toBeNull();
		const bound = {
			eligibility: {
				can_open_pr: false,
				blockers: [...rec.eligibility.blockers, { code: 'binding_unresolved', message: 'No Git source' }]
			}
		};
		expect(deliveryHint(bound, true)).toEqual({ ready: false, text: 'No Git source' });
	});

	it('prefers the workload owner', () => {
		expect(targetName({ owner_kind: 'Deployment', owner_name: 'api', pod_name: 'api-0' })).toBe('Deployment/api');
		expect(targetName({ pod_name: 'api-0' })).toBe('api-0');
		expect(targetName({})).toBe('Cluster-wide');
	});
});
