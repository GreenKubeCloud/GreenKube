/**
 * Savings vocabulary (principle 3: projected ≠ allocated ≠ realized): measured
 * allocated figures are never labeled "Realized" or "avoided", the realization
 * factor is shown, and explicit API fields win over the older ones.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import SavingsHero from '$lib/components/SavingsHero.svelte';
import MeasurementTable from '$lib/components/MeasurementTable.svelte';
import { buildSavingsOverview } from '$lib/overview.js';
import {
	outcomeLabel,
	realizationFactorOf,
	realizationText,
	savingPhrase,
	savingsFigures,
	visibleDiagnostics
} from '$lib/savingsVocabulary.js';
import { stateLabel } from '$lib/changes.js';

const current = {
	realized_to_date: { method: 'measured', basis: 'accrued', cost: 120, co2e_grams: 4000 },
	run_rate_annual: { method: 'measured', basis: 'annual_rate', cost: 900, co2e_grams: 30000 },
	projected_annual: { method: 'projected', basis: 'annual_rate', cost: 500, co2e_grams: 15000 },
	measured_basis: 'measured_allocated',
	realization: {
		measured_allocated_cost_annual: 900,
		measured_allocated_co2e_grams_annual: 30000,
		realization_factor: 0.45,
		realized_billed_cost_annual_expected: 405,
		realized_billed_co2e_grams_annual_expected: 13500,
		realized_billed_measured: false
	},
	applied_count: 3,
	verified_count: 2
};

describe('savingsFigures', () => {
	it('reads the current API: realized_to_date and run_rate_annual are measured allocated figures', () => {
		const figures = savingsFigures(current);
		expect(figures.measuredAllocatedToDate.cost).toBe(120);
		expect(figures.measuredAllocatedAnnual.cost).toBe(900);
		expect(figures.realizationFactor).toBe(0.45);
		expect(figures.expectedBilledCostAnnual).toBe(405);
		// Never derived from an allocated figure.
		expect(figures.realizedBilledToDate).toBeNull();
	});

	it('prefers the explicit measured_allocated, realized_billed and realization_factor fields', () => {
		const figures = savingsFigures({
			...current,
			measured_allocated_to_date: { method: 'measured_allocated', cost: 130, co2e_grams: 4100 },
			measured_allocated_annual: { method: 'measured_allocated', cost: 950, co2e_grams: 31000 },
			realized_billed_to_date: { method: 'realized_billed', cost: 40, co2e_grams: 1000 },
			realization_factor: 0.5
		});
		expect(figures.measuredAllocatedToDate.cost).toBe(130);
		expect(figures.measuredAllocatedAnnual.cost).toBe(950);
		expect(figures.realizedBilledToDate.cost).toBe(40);
		expect(figures.realizationFactor).toBe(0.5);
	});

	it('clamps or drops an invalid realization factor', () => {
		expect(realizationFactorOf({ realization_factor: 1.7 })).toBe(1);
		expect(realizationFactorOf({ realization: { realization_factor: 'n/a' } })).toBeNull();
		expect(realizationFactorOf(null)).toBeNull();
		expect(realizationText(null)).toBe('');
		expect(realizationText(0.45)).toContain('Realization factor 45%');
	});

	it('labels outcomes and change states without calling an allocated saving "Realized"', () => {
		expect(outcomeLabel('realized')).toBe('Saving measured');
		expect(outcomeLabel('not_realized')).toBe('No saving measured');
		expect(outcomeLabel(null)).toBe('—');
		expect(stateLabel('realized')).toBe('Saving measured');
		expect(stateLabel('not_realized')).toBe('No saving measured');
	});
});

describe('MeasurementTable vocabulary', () => {
	const measurement = {
		measurement_id: 'm-1',
		outcome: 'realized',
		decided_at: new Date().toISOString(),
		estimates: [{ quantity: 'allocated_cost', unit: 'currency', claimed: true, annualized: 300, outcome: 'realized' }]
	};

	it('labels the outcome without "Realized" and shows the realization factor when sent', () => {
		const { container } = render(MeasurementTable, {
			props: { measurement: { ...measurement, savings_basis: { realization_factor: 0.3 } } }
		});

		expect(container.textContent).toContain('Saving measured');
		expect(container.textContent).not.toMatch(/\bRealized\b|\brealized\b/);
		expect(screen.getByTestId('measurement-realization')).toHaveTextContent('Realization factor 30%');
	});

	it('works without savings_basis (older server)', () => {
		render(MeasurementTable, { props: { measurement } });

		expect(screen.queryByTestId('measurement-realization')).not.toBeInTheDocument();
	});
});

describe('SavingsHero vocabulary', () => {
	it('shows measured allocated savings, allocated CO2e and the realization factor', () => {
		const { container } = render(SavingsHero, { props: { overview: buildSavingsOverview(current, []) } });

		const card = screen.getByTestId('savings-realized');
		expect(card).toHaveTextContent('Measured allocated savings');
		expect(screen.getByTestId('realized-co2e')).toHaveTextContent('Allocated CO₂e');
		expect(screen.getByTestId('realized-run-rate')).toHaveTextContent('Measured allocated run rate');
		expect(screen.getByTestId('realization-factor')).toHaveTextContent('Realization factor 45%');
		expect(screen.getByTestId('realization-factor')).toHaveTextContent('$405/yr');
		expect(container.textContent).not.toMatch(/Realized savings|CO₂e avoided/);
		expect(screen.queryByTestId('realized-billed')).not.toBeInTheDocument();
	});

	it('reserves "Realized" for billed savings when the server reports them', () => {
		const overview = buildSavingsOverview(
			{ ...current, realized_billed_to_date: { method: 'realized_billed', cost: 40, co2e_grams: 1000 } },
			[]
		);
		render(SavingsHero, { props: { overview } });

		expect(screen.getByTestId('realized-billed')).toHaveTextContent('Realized (billed) savings: $40');
	});

	it('works without the realization block (older server)', () => {
		const { realization, ...older } = current;
		render(SavingsHero, { props: { overview: buildSavingsOverview(older, []) } });

		expect(realization).toBeTruthy();
		expect(screen.getByTestId('realized-cost')).toHaveTextContent('$120');
		expect(screen.queryByTestId('realization-factor')).not.toBeInTheDocument();
	});
});

describe('savingPhrase', () => {
	const money = (value) => '$' + value.toFixed(2);
	const kg = (grams) => (grams / 1000).toFixed(2) + ' kg';

	it('reads a saving as a positive amount, never with a minus sign', () => {
		expect(savingPhrase(232.53, money)).toBe('saves $232.53/yr');
		expect(savingPhrase(69900, kg, { suffix: ' CO₂e/yr' })).toBe('saves 69.90 kg CO₂e/yr');
		expect(savingPhrase(580, money, { verb: 'saved' })).toBe('saved $580.00/yr');
	});

	it('keeps an increase as an increase', () => {
		expect(savingPhrase(-40, money)).toBe('costs $40.00 more/yr');
		expect(savingPhrase(-40, money, { verb: 'saved' })).toBe('cost $40.00 more/yr');
	});

	it('returns null for missing or zero values', () => {
		expect(savingPhrase(null, money)).toBeNull();
		expect(savingPhrase(undefined, money)).toBeNull();
		expect(savingPhrase(0, money)).toBeNull();
	});
});

describe('visibleDiagnostics', () => {
	const eligibility = {
		blockers: [
			{ code: 'action_unavailable', message: 'No typed change' },
			{ code: 'binding_unresolved', message: 'No Git source' }
		],
		warnings: [{ code: 'automation_not_configured', message: 'No plan service' }]
	};

	it('shows every diagnostic on a real install', () => {
		expect(visibleDiagnostics(eligibility)).toEqual({ blockers: eligibility.blockers, warnings: eligibility.warnings });
	});

	it('hides only the automation-unavailable diagnostics in demo mode', () => {
		expect(visibleDiagnostics(eligibility, true)).toEqual({
			blockers: [{ code: 'binding_unresolved', message: 'No Git source' }],
			warnings: []
		});
		expect(visibleDiagnostics(null, true)).toEqual({ blockers: [], warnings: [] });
	});
});
