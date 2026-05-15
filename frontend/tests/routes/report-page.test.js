/**
 * Tests for the report route controls.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import ReportPage from '../../src/routes/report/+page.svelte';


vi.mock('$lib/api.js', () => ({
	getNamespaces: vi.fn(() => Promise.resolve(['default', 'prod'])),
	getReportYears: vi.fn(() => Promise.resolve([2026, 2025])),
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

	it('shows custom date controls when custom range is selected', async () => {
		render(ReportPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Custom' }));

		expect(screen.getByLabelText('Start')).toBeInTheDocument();
		expect(screen.getByLabelText('End')).toBeInTheDocument();
	});

	it('shows data-backed year choices for yearly selection', async () => {
		render(ReportPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Years' }));

		expect(await screen.findByRole('button', { name: '2026' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: '2025' })).toBeInTheDocument();
	});

	it('offers namespace and pod grouping controls', async () => {
		render(ReportPage);
		expect(await screen.findByRole('button', { name: 'Pod' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Namespace' })).toBeInTheDocument();
	});
});
