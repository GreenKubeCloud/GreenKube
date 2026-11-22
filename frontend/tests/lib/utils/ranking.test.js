/**
 * Ranking presentation helpers (K2): score formatting and top factor.
 */
import { describe, expect, it } from 'vitest';
import {
	describeRankingFactors,
	formatRankingScore,
	rankingFactorLabel,
	rankingFactorValue,
	sortedRankingFactors,
	topRankingFactor
} from '#lib/utils/ranking.js';

const factors = {
	carbon: 0.12,
	cost: 0.3,
	confidence: 0.135,
	risk: 0.075,
	effort: 0.05,
	actionability: 0,
	source: 0
};

describe('topRankingFactor', () => {
	it('returns the factor contributing most to the score', () => {
		expect(topRankingFactor(factors)).toEqual({ key: 'cost', label: 'Cost savings', value: 0.3 });
	});

	it('accepts object entries and numeric strings from later ranking versions', () => {
		expect(
			topRankingFactor({ value: { value: 412.5, unit: 'USD' }, effort: { contribution: -30 }, risk: '12' })
		).toMatchObject({ key: 'value', label: 'Expected value', value: 412.5 });
	});

	it('returns null when nothing contributes', () => {
		expect(topRankingFactor({ carbon: 0, cost: 0 })).toBeNull();
		expect(topRankingFactor({})).toBeNull();
		expect(topRankingFactor(null)).toBeNull();
		expect(topRankingFactor(['cost'])).toBeNull();
		expect(topRankingFactor({ cost: 'n/a', carbon: Number.NaN })).toBeNull();
	});
});

describe('sortedRankingFactors', () => {
	it('orders by contribution and breaks ties by key', () => {
		expect(sortedRankingFactors({ b: 1, a: 1, c: 2 }).map((factor) => factor.key)).toEqual(['c', 'a', 'b']);
	});
});

describe('rankingFactorLabel', () => {
	it('labels known factors and humanizes unknown ones', () => {
		expect(rankingFactorLabel('risk')).toBe('Low risk');
		expect(rankingFactorLabel('carbon')).toBe('Carbon value');
		expect(rankingFactorLabel('blast_radius')).toBe('Blast radius');
		expect(rankingFactorLabel('effort')).toBe('Effort');
		expect(rankingFactorLabel('incident_risk')).toBe('Incident risk');
		expect(rankingFactorLabel('reliability')).toBe('Incidents avoided');
		expect(rankingFactorLabel('')).toBe('Unknown factor');
		expect(rankingFactorLabel(undefined)).toBe('Unknown factor');
	});
});

describe('rankingFactorValue', () => {
	it('reads numbers, numeric strings and value-like objects', () => {
		expect(rankingFactorValue(0.2)).toBe(0.2);
		expect(rankingFactorValue('0.4')).toBe(0.4);
		expect(rankingFactorValue({ amount: 3 })).toBe(3);
		expect(rankingFactorValue({})).toBeNull();
		expect(rankingFactorValue(Infinity)).toBeNull();
		expect(rankingFactorValue('')).toBeNull();
	});
});

describe('formatRankingScore', () => {
	it('formats with two decimals', () => {
		expect(formatRankingScore(0.6837)).toBe('0.68');
		expect(formatRankingScore('0.5')).toBe('0.50');
	});

	it('shows a dash when there is no score', () => {
		expect(formatRankingScore(null)).toBe('—');
		expect(formatRankingScore(undefined)).toBe('—');
		expect(formatRankingScore('')).toBe('—');
	});
});

describe('describeRankingFactors', () => {
	it('lists every numeric factor, largest first', () => {
		expect(describeRankingFactors({ cost: 0.3, risk: 0.081, note: 'x' })).toBe('Cost savings 0.30 · Low risk 0.08');
		expect(describeRankingFactors(undefined)).toBe('');
	});
});
