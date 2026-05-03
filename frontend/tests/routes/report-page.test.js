/**
 * Tests for the report route controls.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import ReportPage from '../../src/routes/report/+page.svelte';


vi.mock('$lib/api.js', () => ({
	getNamespaces: vi.fn(() => Promise.resolve(['default', 'prod'])),
	getReportSummary: vi.fn(() => Promise.resolve({
		total_rows: 1,
		unique_pods: 1,
		unique_namespaces: 1,
		total_co2e_grams: 1,
		total_embodied_co2e_grams: 1,
		total_cost: 1,
		total_energy_joules: 1
	})),
	buildReportExportUrl: vi.fn(() => 'http://localhost:3000/api/v1/report/export?format=csv')
}));


describe('ReportPage', () => {
	beforeEach(() => {
		delete globalThis.window;
		globalThis.window = { location: { origin: 'http://localhost:3000' } };
	});

	afterEach(() => {
		vi.clearAllMocks();
	});

	it('renders the YTD time window option', async () => {
		render(ReportPage);
		expect(await screen.findByRole('button', { name: 'Year to date' })).toBeInTheDocument();
	});

	it('shows aggregation levels without depending on the selected window', async () => {
		render(ReportPage);
		expect(await screen.findByRole('button', { name: 'Hourly' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Daily' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Weekly' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Monthly' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Yearly' })).toBeInTheDocument();
	});
});
