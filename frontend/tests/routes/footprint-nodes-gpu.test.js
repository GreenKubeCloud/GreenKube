/**
 * NRG-13: nodes whose GPU power is estimated from the TDP or not measured at
 * all carry a visible badge on the Footprint nodes page.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$lib/api.js', async (importOriginal) => {
	const actual = await importOriginal();
	return { ...actual, getNodes: vi.fn() };
});

import * as api from '$lib/api.js';
import { gpuPowerBadge } from '$lib/nodeGpu.js';
import NodesPage from '../../src/routes/footprint/nodes/+page.svelte';

describe('gpuPowerBadge', () => {
	it('flags estimated and unmeasured GPU power, and nothing else', () => {
		expect(gpuPowerBadge({ gpu_source: 'gpu_tdp_estimate' })).toMatchObject({ label: 'GPU power estimated', tone: 'yellow' });
		expect(gpuPowerBadge({ gpu_source: 'unmeasured' })).toMatchObject({ label: 'GPU power not measured', tone: 'red' });
		expect(gpuPowerBadge({ gpu_source: 'dcgm' })).toBeNull();
		expect(gpuPowerBadge({ gpu_source: null })).toBeNull();
		expect(gpuPowerBadge({})).toBeNull();
	});
});

describe('Footprint nodes page', () => {
	afterEach(() => vi.clearAllMocks());

	it('shows a GPU power badge on nodes without DCGM', async () => {
		api.getNodes.mockResolvedValue([
			{ name: 'gpu-estimated', gpu_source: 'gpu_tdp_estimate' },
			{ name: 'gpu-unmeasured', gpu_source: 'unmeasured' },
			{ name: 'gpu-dcgm', gpu_source: 'dcgm' },
			{ name: 'cpu-only', gpu_source: null }
		]);
		render(NodesPage);
		await waitFor(() => expect(screen.getByText('gpu-estimated')).toBeInTheDocument());
		const estimated = screen.getByText('GPU power estimated');
		expect(estimated).toHaveAttribute('title', expect.stringContaining('DCGM'));
		expect(screen.getByText('GPU power not measured')).toBeInTheDocument();
		expect(screen.getAllByText(/^GPU power (estimated|not measured)$/)).toHaveLength(2);
		expect(screen.getByRole('status')).toHaveTextContent('2 nodes');
	});
});
