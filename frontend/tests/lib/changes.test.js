import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', async () => {
	class ApiError extends Error {
		constructor(message, status) {
			super(message);
			this.name = 'ApiError';
			this.status = status;
		}
	}
	return { ApiError, getChange: vi.fn(), listChanges: vi.fn() };
});

import * as api from '$lib/api.js';
import {
	CHANGE_SCAN_PAGES,
	changeTarget,
	latestMeasurement,
	measurementRows,
	quantityLabel,
	resolveChange,
	stateLabel,
	stateTone
} from '$lib/changes.js';

const notFound = () => new api.ApiError('Change not found.', 404);

describe('resolveChange', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('opens a change id directly', async () => {
		api.getChange.mockResolvedValue({ change_id: 'chg-1' });
		await expect(resolveChange('chg-1')).resolves.toEqual({ change_id: 'chg-1' });
		expect(api.listChanges).not.toHaveBeenCalled();
	});

	it('finds the change of a plan id among recent changes (PR deep link /ledger?change={plan_id})', async () => {
		api.getChange.mockImplementation(async (id) => {
			if (id === 'chg-7') return { change_id: 'chg-7', plan_id: 'plan-7' };
			throw notFound();
		});
		api.listChanges
			.mockResolvedValueOnce({ items: [{ change_id: 'chg-1', plan_id: 'plan-1' }], next_cursor: 'c1' })
			.mockResolvedValueOnce({ items: [{ change_id: 'chg-7', plan_id: 'plan-7' }], next_cursor: null });

		const change = await resolveChange('plan-7');

		expect(change).toEqual({ change_id: 'chg-7', plan_id: 'plan-7' });
		expect(api.listChanges).toHaveBeenCalledTimes(2);
		expect(api.listChanges.mock.calls[1][0].cursor).toBe('c1');
	});

	it('looks the plan up first when asked and falls back to a change id', async () => {
		api.listChanges.mockResolvedValue({ items: [], next_cursor: null });
		api.getChange.mockResolvedValue({ change_id: 'chg-2' });

		await expect(resolveChange('chg-2', { preferPlan: true })).resolves.toEqual({ change_id: 'chg-2' });
		expect(api.listChanges).toHaveBeenCalledTimes(1);
	});

	it('stops after a bounded number of pages and returns null when nothing matches', async () => {
		api.getChange.mockRejectedValue(notFound());
		api.listChanges.mockResolvedValue({ items: [{ change_id: 'x', plan_id: 'other' }], next_cursor: 'more' });

		await expect(resolveChange('plan-missing')).resolves.toBeNull();
		expect(api.listChanges).toHaveBeenCalledTimes(CHANGE_SCAN_PAGES);
	});

	it('propagates server errors instead of reporting a missing change', async () => {
		api.getChange.mockRejectedValue(new api.ApiError('Changes are temporarily unavailable.', 503));
		await expect(resolveChange('chg-1')).rejects.toThrow('temporarily unavailable');
	});

	it('returns null without an id', async () => {
		await expect(resolveChange('')).resolves.toBeNull();
		expect(api.getChange).not.toHaveBeenCalled();
	});
});

describe('measurementRows', () => {
	it('lists claimed quantities first and keeps measured and projected values apart', () => {
		const rows = measurementRows({
			estimates: [
				{ quantity: 'core_hours', unit: 'core-hours', claimed: false, annualized: 120 },
				{
					quantity: 'allocated_cost',
					unit: 'currency',
					claimed: true,
					annualized: 300,
					annualized_ci_low: 250,
					annualized_ci_high: 350,
					projected_annual: 400,
					outcome: 'realized'
				}
			]
		});

		expect(rows.map((row) => row.quantity)).toEqual(['allocated_cost', 'core_hours']);
		expect(rows[0]).toMatchObject({
			label: 'Allocated cost',
			kind: 'cost',
			claimed: true,
			annualized: 300,
			ciLow: 250,
			ciHigh: 350,
			projectedAnnual: 400,
			outcome: 'realized'
		});
		expect(rows[1]).toMatchObject({ kind: 'unit', claimed: false, projectedAnnual: null });
	});

	it('is empty for a measurement without estimates', () => {
		expect(measurementRows(null)).toEqual([]);
		expect(measurementRows({ estimates: 'bad' })).toEqual([]);
	});
});

describe('latestMeasurement', () => {
	it('returns the measurement nothing supersedes', () => {
		const first = { measurement_id: 'm1' };
		const second = { measurement_id: 'm2', supersedes_measurement_id: 'm1' };
		expect(latestMeasurement([first, second])).toBe(second);
		expect(latestMeasurement([])).toBeNull();
	});
});

describe('labels', () => {
	it('names every measurement state in words and falls back for unknown ones', () => {
		expect(stateLabel('regressed')).toBe('Regressed (rollback review)');
		expect(stateLabel('some_new_state')).toBe('some new state');
		expect(stateLabel(null)).toBe('Not tracked yet');
		expect(stateTone('realized')).toBe('green');
		expect(stateTone('nope')).toBe('dark');
		expect(quantityLabel('co2e_g')).toBe('Operational CO₂e');
	});

	it('names the change target', () => {
		expect(changeTarget({ target: { kind: 'Deployment', namespace: 'shop', name: 'api' } })).toBe('Deployment shop/api');
		expect(changeTarget({})).toBe('Unknown workload');
	});
});
