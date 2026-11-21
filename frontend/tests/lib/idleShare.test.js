/**
 * Footprint idle share (WP-05.3, NRG-02): allocated totals, the node idle
 * share and the cluster totals side by side; "not available" when unknown.
 */
import { describe, expect, it } from 'vitest';
import { NOT_AVAILABLE, idleShareNote, idleShareRows } from '../../src/lib/idleShare.js';

const known = {
	total_co2e_grams: 1000,
	total_embodied_co2e_grams: 200,
	total_co2e_all_scopes: 1200,
	total_cost: 3,
	total_energy_joules: 3.6e6,
	idle_co2e_grams: 500,
	idle_embodied_co2e_grams: 100,
	idle_cost: 1,
	idle_energy_joules: 7.2e6,
	cluster_total_co2e_grams: 1500,
	cluster_total_embodied_co2e_grams: 300,
	cluster_total_cost: 4,
	cluster_total_energy_joules: 1.08e7,
	idle_data_since: '2026-11-01T00:00:00Z'
};

describe('idleShareRows', () => {
	it('lists CO2e (both scopes), cost and energy with allocated, idle and cluster totals', () => {
		const rows = idleShareRows(known);
		expect(rows.map((row) => row.key)).toEqual(['co2e', 'cost', 'energy']);
		const [co2e, cost, energy] = rows;
		expect(co2e).toMatchObject({ allocated: 1200, idle: 600, total: 1800 });
		expect(cost).toMatchObject({ allocated: 3, idle: 1, total: 4 });
		expect(energy).toMatchObject({ allocated: 3.6e6, idle: 7.2e6, total: 1.08e7 });
		expect(co2e.idleShare).toBeCloseTo(600 / 1800);
	});

	it('leaves the idle share and the cluster total unknown when the API says null', () => {
		const rows = idleShareRows({ ...known, idle_co2e_grams: null, idle_embodied_co2e_grams: null, idle_cost: null, idle_energy_joules: null, cluster_total_co2e_grams: null, cluster_total_embodied_co2e_grams: null, cluster_total_cost: null, cluster_total_energy_joules: null });
		for (const row of rows) {
			expect(row.idle).toBeNull();
			expect(row.total).toBeNull();
			expect(row.idleShare).toBeNull();
			expect(row.allocated).not.toBeNull();
		}
	});

	it('treats missing fields (an older summary cache row) as unknown, never zero', () => {
		const rows = idleShareRows({ total_co2e_grams: 5, total_cost: 1, total_energy_joules: 2 });
		expect(rows.every((row) => row.idle === null && row.total === null)).toBe(true);
	});
});

describe('idleShareNote', () => {
	it('says why the idle share is not available', () => {
		expect(idleShareNote(known, '')).toBe('');
		expect(idleShareNote({}, 'shop')).toMatch(/no namespace/);
		expect(idleShareNote({ idle_data_since: '2026-11-01T00:00:00Z' }, '')).toMatch(/idle history starts/i);
		expect(idleShareNote({}, '')).toMatch(/no node idle row/i);
	});

	it('exports the not-available label', () => {
		expect(NOT_AVAILABLE).toBe('not available');
	});
});
