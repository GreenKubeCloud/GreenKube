/**
 * Savings hero of the Overview (K1): realized (measured) and projected
 * figures side by side, clearly labeled and never summed.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import SavingsHero from '$lib/components/SavingsHero.svelte';
import { buildSavingsOverview } from '$lib/overview.js';

const savings = {
	realized_to_date: { method: 'measured', basis: 'accrued', cost: 120, co2e_grams: 4000 },
	run_rate_annual: { method: 'measured', basis: 'annual_rate', cost: 900, co2e_grams: 30000 },
	projected_annual: { method: 'projected', basis: 'annual_rate', cost: 500, co2e_grams: 15000 },
	applied_count: 3,
	verified_count: 2,
	diagnostics: ['Cost source: pricing catalog.']
};

const recommendations = [
	{ id: 1, potential_savings_cost: 300, potential_savings_co2e_grams: 9000, value: { ev: 250, currency: 'USD' } },
	{ id: 2, potential_savings_cost: 0, value: { ev: 20, lane: 'reliability', currency: 'USD' } }
];

describe('SavingsHero', () => {
	it('shows realized, pending and opportunity as separate labeled figures', () => {
		render(SavingsHero, { props: { overview: buildSavingsOverview(savings, recommendations) } });

		expect(screen.getByTestId('realized-cost')).toHaveTextContent('$120');
		expect(screen.getByTestId('realized-run-rate')).toHaveTextContent('$900/yr');
		expect(screen.getByTestId('pending-cost')).toHaveTextContent('$500/yr');
		expect(screen.getByTestId('opportunity-cost')).toHaveTextContent('$300/yr');
		expect(screen.getByTestId('opportunity-ev')).toHaveTextContent('$250/yr');
		expect(screen.getByTestId('savings-realized')).toHaveTextContent('Measured');
		expect(screen.getByTestId('savings-pending')).toHaveTextContent('Projected');
		expect(screen.getByTestId('savings-opportunity')).toHaveTextContent('Projected');
		expect(screen.getByTestId('never-summed-note')).toHaveTextContent('never added together');
		expect(screen.getByText(/1 reliability recommendation adds resources/)).toBeInTheDocument();
		expect(screen.getByText(/Cost source: pricing catalog/)).toBeInTheDocument();
	});

	it('never shows a total across measured and projected savings', () => {
		const { container } = render(SavingsHero, { props: { overview: buildSavingsOverview(savings, recommendations) } });
		const text = container.textContent;
		// 120 + 500 + 300, 900 + 500 + 300, 120 + 500, 900 + 500: none may appear.
		for (const total of ['$920', '$1,700', '$620', '$1,400']) {
			expect(text).not.toContain(total);
		}
		expect(text.toLowerCase()).not.toContain('total');
	});

	it('explains the empty states after an install', () => {
		render(SavingsHero, { props: { overview: buildSavingsOverview({}, []) } });
		expect(screen.getByTestId('realized-empty')).toHaveTextContent('No measured savings yet');
		expect(screen.getByTestId('pending-empty')).toBeInTheDocument();
		expect(screen.getByTestId('opportunity-empty')).toBeInTheDocument();
	});

	it('shows loading and error states', () => {
		const { unmount } = render(SavingsHero, { props: { overview: null, loading: true } });
		expect(screen.getAllByRole('status')).toHaveLength(3);
		unmount();
		render(SavingsHero, {
			props: { overview: buildSavingsOverview(null, []), savingsError: 'Savings are temporarily unavailable.' }
		});
		expect(screen.getByRole('alert')).toHaveTextContent('Savings are temporarily unavailable.');
	});
});
