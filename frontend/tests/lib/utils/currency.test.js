/**
 * Every amount is shown in the pricing currency (impact.pricing.currency,
 * exposed as GET /config `currency`), never with a hard-coded "$".
 */
import { afterEach, describe, expect, it } from 'vitest';
import {
	currencyOrDefault,
	formatCost,
	getDisplayCurrency,
	setDisplayCurrency
} from '../../../src/lib/utils/format.js';
import { formatMoney } from '../../../src/lib/utils/decision.js';
import { currencyOf } from '../../../src/lib/overview.js';
import { impactOf, valueOf } from '../../../src/lib/recommendationDetail.js';
import { buildMultiSeriesOption } from '../../../src/lib/charts.js';
import { currencySymbol } from '../../../src/lib/utils/format.js';

afterEach(() => {
	setDisplayCurrency('USD');
});

describe('display currency', () => {
	it('defaults to USD and keeps formatCost backward compatible', () => {
		expect(getDisplayCurrency()).toBe('USD');
		expect(formatCost(12.5)).toBe('$12.50');
	});

	it('formats costs in EUR once the server says so', () => {
		expect(setDisplayCurrency('EUR')).toBe('EUR');
		expect(formatCost(12.5)).toBe('€12.50');
		expect(formatCost(0.123)).toBe('€0.123');
		expect(formatCost(0.0012)).toBe('€0.0012');
		expect(formatCost(1234.5)).not.toContain('$');
		expect(formatMoney(412)).toBe('€412');
		expect(formatMoney(3.5)).toBe('€3.50');
	});

	it('accepts lower-case codes and ignores invalid ones', () => {
		expect(setDisplayCurrency('gbp')).toBe('GBP');
		expect(setDisplayCurrency('euros')).toBe('GBP');
		expect(setDisplayCurrency(undefined)).toBe('GBP');
		expect(formatCost(2)).toBe('£2.00');
	});

	it('an explicit currency on a value wins over the display currency', () => {
		setDisplayCurrency('EUR');
		expect(formatCost(2, 'USD')).toBe('$2.00');
		expect(formatMoney(2, 'USD')).toBe('$2.00');
		expect(currencyOrDefault('bogus')).toBe('EUR');
	});

	it('formats negative amounts with the magnitude rule', () => {
		setDisplayCurrency('EUR');
		expect(formatCost(-2)).toBe('-€2.00');
	});

	it('falls back to the display currency where the API omits one', () => {
		setDisplayCurrency('EUR');
		expect(currencyOf([])).toBe('EUR');
		expect(currencyOf([{ value: { currency: 'USD' } }])).toBe('USD');
		expect(impactOf({ impact: { allocated_cost_annual: 1 } }).currency).toBe('EUR');
		expect(valueOf({ value: { ev: 1 } }).currency).toBe('EUR');
	});

	it('labels the cost chart series with the currency symbol', () => {
		setDisplayCurrency('EUR');
		expect(currencySymbol()).toBe('€');
		const option = buildMultiSeriesOption([{ timestamp: 't', co2e_grams: 1, total_cost: 1, joules: 1000 }]);
		expect(option.series[1].name).toBe('Cost (€)');
		expect(option.legend.data).toContain('Cost (€)');
	});
});
