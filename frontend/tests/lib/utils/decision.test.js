import { describe, expect, it } from 'vitest';
import {
	describeExpectedValue,
	eligibilityOf,
	expectedValue,
	formatMoney,
	isReliabilityLane,
	planTargetFor,
	successProbability
} from '../../../src/lib/utils/decision.js';

describe('expected value helpers (I1)', () => {
	it('reads the server value', () => {
		const rec = { value: { ev: 412.4, p_success: 0.82, currency: 'EUR', lane: 'savings' } };
		expect(expectedValue(rec)).toBe(412.4);
		expect(successProbability(rec)).toBe(0.82);
		expect(describeExpectedValue(rec)).toBe('≈ €412/yr expected · 82% success');
		expect(isReliabilityLane(rec)).toBe(false);
	});

	it('returns null without a value', () => {
		expect(expectedValue({})).toBeNull();
		expect(describeExpectedValue({ ranking_score: 0.4 })).toBeNull();
		expect(describeExpectedValue({ value: { ev: 'n/a' } })).toBeNull();
	});

	it('formats small, negative and unknown-currency amounts', () => {
		expect(formatMoney(3.5, 'USD')).toBe('$3.50');
		expect(formatMoney(-12.4, 'USD')).toBe('-$12');
		expect(formatMoney(20, 'not-a-code')).toBe('$20');
		expect(formatMoney(null)).toBe('—');
	});

	it('flags the reliability lane', () => {
		expect(isReliabilityLane({ value: { lane: 'reliability' } })).toBe(true);
	});
});

describe('eligibility helpers (F10)', () => {
	const eligible = {
		id: 7,
		eligibility: {
			can_open_pr: true,
			delivery_modes: ['direct', 'manual'],
			blockers: [],
			warnings: [{ code: 'singleton_workload', message: 'One replica.', fix: 'Add a replica.' }],
			recommendation_ids: [7, 9]
		}
	};

	it('normalizes the server block', () => {
		const result = eligibilityOf(eligible);
		expect(result.canOpenPr).toBe(true);
		expect(result.deliveryModes).toEqual(['direct', 'manual']);
		expect(result.warnings).toEqual([{ code: 'singleton_workload', message: 'One replica.', fix: 'Add a replica.' }]);
		expect(eligibilityOf({})).toBeNull();
	});

	it('plans the server change set only when a PR can be opened', () => {
		expect(planTargetFor(eligible)).toEqual({ recommendationIds: [7, 9] });
		expect(planTargetFor({ ...eligible, eligibility: { ...eligible.eligibility, can_open_pr: false } })).toBeNull();
		expect(planTargetFor({ id: 7 })).toBeNull();
	});

	it('defaults the change set to the recommendation and rejects foreign sets', () => {
		expect(planTargetFor({ id: 7, eligibility: { can_open_pr: true } })).toEqual({ recommendationIds: [7] });
		expect(planTargetFor({ id: 7, eligibility: { can_open_pr: true, recommendation_ids: [8] } })).toBeNull();
		expect(planTargetFor({ id: 'x', eligibility: { can_open_pr: true } })).toBeNull();
	});

	it('drops malformed blockers', () => {
		const result = eligibilityOf({ eligibility: { blockers: [null, { message: 'no code' }, { code: 'warming_up' }] } });
		expect(result.blockers).toEqual([{ code: 'warming_up', message: '', fix: '' }]);
	});
});
