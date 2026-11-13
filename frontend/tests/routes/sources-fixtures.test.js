/**
 * The Sources page (K6) on the JSON the API really returns:
 * `GET /api/v1/sources` and `GET /api/v1/sources/{id}/runs`, generated from
 * the FastAPI app by tests/api/test_ui_contract_fixtures.py.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/svelte';
import SourcesPage from '../../src/routes/sources/+page.svelte';
import * as api from '$lib/api.js';
import { sortSources, sourceStatus } from '$lib/sources.js';
import SOURCES_FIXTURE from '../fixtures/api/sources_list.json';
import RUNS_FIXTURE from '../fixtures/api/source_runs.json';

vi.mock('$lib/api.js', () => ({
	listSources: vi.fn(),
	listSourceRuns: vi.fn(),
	getSourceTrackRecord: vi.fn()
}));

describe('/sources on the API contract fixtures', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.listSources.mockResolvedValue(SOURCES_FIXTURE);
		api.listSourceRuns.mockResolvedValue(RUNS_FIXTURE);
		api.getSourceTrackRecord.mockResolvedValue({ entries: [] });
	});

	it('knows every status word the API returns', () => {
		const statuses = new Set(SOURCES_FIXTURE.sources.map((source) => source.status));
		for (const status of statuses) expect(sourceStatus(status).label).not.toBe(status);
		expect([...statuses].sort()).toEqual(['degraded', 'disabled', 'failing', 'pending', 'unregistered']);
	});

	it('lists failing sources first, then degraded ones, with reasons in words', async () => {
		render(SourcesPage);

		const list = await screen.findByTestId('sources-list');
		const ids = within(list).getAllByRole('listitem').filter((item) => item.dataset.testid).map((item) => item.dataset.testid);
		expect(ids.slice(0, 3)).toEqual(['source-vpa', 'source-greenkube.orphaned_lb', 'source-greenkube.rightsizing_cpu']);
		expect(ids).toHaveLength(SOURCES_FIXTURE.sources.length);
		expect(ids).toEqual(sortSources(SOURCES_FIXTURE.sources).map((source) => `source-${source.id}`));

		const vpa = screen.getByTestId('source-vpa');
		expect(vpa).toHaveTextContent('Failing');
		expect(vpa).toHaveTextContent('enabled explicitly but unavailable: VPA CRD not installed');
		expect(screen.getByTestId('source-greenkube.orphaned_lb')).toHaveTextContent('RuntimeError');
		expect(screen.getByTestId('source-greenkube.rightsizing_cpu')).toHaveTextContent('Degraded');
		expect(screen.getByTestId('source-greenkube.zombie')).toHaveTextContent('Not run yet');
		expect(screen.getByTestId('source-greenkube.carbon_aware')).toHaveTextContent('Disabled');
		expect(screen.getByTestId('source-uninstalled')).toHaveTextContent('Unregistered');
		expect(screen.getByTestId('source-problems')).toHaveTextContent("No module named 'acme_sdk'");
	});

	it('shows the recent runs of a source with their diagnostics', async () => {
		render(SourcesPage);

		const source = await screen.findByTestId('source-greenkube.rightsizing_cpu');
		expect(source).toHaveTextContent('Signals3');
		await fireEvent.click(within(source).getByRole('button', { name: 'Show recent runs' }));

		const runs = await within(source).findByTestId('runs-greenkube.rightsizing_cpu');
		expect(api.listSourceRuns).toHaveBeenCalledWith('greenkube.rightsizing_cpu', expect.objectContaining({ limit: 20 }));
		const rows = within(runs).getAllByRole('row').slice(1);
		expect(rows).toHaveLength(RUNS_FIXTURE.length);
		expect(rows[0]).toHaveTextContent('incomplete');
		expect(rows[0]).toHaveTextContent('1.3 s');
		expect(rows[0]).toHaveTextContent('warning: 12 VPAs skipped: updateMode=Auto');
		expect(rows[1]).toHaveTextContent('succeeded');
		expect(rows[1]).toHaveTextContent('12');
	});
});
