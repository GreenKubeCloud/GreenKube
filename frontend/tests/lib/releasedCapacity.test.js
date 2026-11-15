import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import ReleasedCapacity from '$lib/components/ReleasedCapacity.svelte';
import { capacitySummary, formatCapacity, releasedCapacityOf } from '$lib/releasedCapacity.js';

const projected = {
	method: 'projected',
	period: 'year',
	annual: { cpu_core_hours: 15768, mem_gib_hours: 0, replica_hours: 17520, node_hours: 0 },
	instantaneous: { cpu_cores_per_replica: 0.9, cpu_cores: 1.8, replicas: 2 },
	direction: 'release',
	note: 'Reserved capacity, not money: never added to cost savings.'
};

describe('releasedCapacityOf', () => {
	it('maps the projected figure with units and the projected label', () => {
		const view = releasedCapacityOf(projected);
		expect(view.method).toBe('projected');
		expect(view.period).toBe('per year');
		expect(view.rows.find((row) => row.key === 'cpu_cores').text).toBe('1.80 cores');
		expect(view.rows.find((row) => row.key === 'cpu_core_hours').text).toBe('15,768 core-hours');
		expect(view.rows.some((row) => row.key === 'mem_gib_hours')).toBe(false);
	});

	it('keeps increases negative and never calls them a saving', () => {
		const view = releasedCapacityOf({
			method: 'projected',
			instantaneous: { mem_gib: -1 },
			annual: { mem_gib_hours: -8760 },
			direction: 'increase'
		});
		expect(view.increase).toBe(true);
		expect(view.rows[0].text).toBe('−1.00 GiB');
		expect(capacitySummary({ method: 'projected', instantaneous: { mem_gib: -1 } })).toBe('Reserves 1.00 GiB more (projected)');
	});

	it('maps a measured figure with its verdict and replica change', () => {
		const view = releasedCapacityOf({
			method: 'measured',
			verdict: 'released',
			per_replica: { cpu_cores: 0.9, mem_gib: 0 },
			total: { cpu_cores: 0.9, mem_gib: 0 },
			cpu_core_hours: 151.2,
			replica_change: { before: 1, after: 2 }
		});
		expect(view.label).toBe('Measured');
		expect(view.verdict).toBe('released');
		expect(view.replicaChange.after).toBe(2);
		expect(view.rows.find((row) => row.key === 'cpu_core_hours_measured').text).toBe('151 core-hours');
	});

	it('returns null without a figure', () => {
		expect(releasedCapacityOf(null)).toBeNull();
		expect(capacitySummary({ instantaneous: {} })).toBeNull();
		expect(formatCapacity(null, 'cores')).toBe('—');
	});
});

describe('ReleasedCapacity component', () => {
	it('renders the projected block with its label, units and the not-money note', () => {
		render(ReleasedCapacity, { props: { capacity: projected } });
		expect(screen.getByText('Released capacity (projected)')).toBeTruthy();
		expect(screen.getByText('1.80 cores')).toBeTruthy();
		expect(screen.getByText(/never added to cost savings/)).toBeTruthy();
	});

	it('renders the measured verdict and the separate replica change', () => {
		render(ReleasedCapacity, {
			props: {
				capacity: { method: 'measured', verdict: 'released', total: { cpu_cores: 0.9 }, replica_change: { before: 1, after: 2 } },
				testid: 'measured-capacity'
			}
		});
		expect(screen.getByTestId('measured-capacity-verdict').textContent).toBe('Released');
		expect(screen.getByTestId('measured-capacity-replicas').textContent).toMatch(/1 → 2 replicas/);
	});
});
