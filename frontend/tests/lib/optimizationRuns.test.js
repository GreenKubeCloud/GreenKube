/**
 * Recommendation scan runs (E1 / K9, BUG-25): request, feature detection,
 * polling with backoff, timeout and abort.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '$lib/api.js';
import {
	findPendingOptimizationRun,
	isRunPending,
	isRunTerminal,
	normalizeRun,
	optimizationRunListSupported,
	optimizationRunsSupported,
	resetOptimizationRunsSupport,
	sleep,
	startOptimizationScan,
	waitForOptimizationRun
} from '$lib/optimizationRuns.js';

vi.mock('$lib/api.js', () => ({
	getOptimizationRun: vi.fn(),
	listOptimizationRuns: vi.fn(),
	requestOptimizationRun: vi.fn()
}));

function apiError(status, message = `API error ${status}`) {
	return Object.assign(new Error(message), { name: 'ApiError', status });
}

beforeEach(() => {
	vi.clearAllMocks();
	resetOptimizationRunsSupport();
});

afterEach(() => {
	vi.useRealTimers();
});

describe('run helpers', () => {
	it('normalizes run ids and statuses', () => {
		expect(normalizeRun({ id: 4, status: 'RUNNING' })).toMatchObject({ run_id: 4, status: 'running' });
		expect(normalizeRun({ run_id: 5 })).toMatchObject({ run_id: 5, status: 'queued' });
		expect(normalizeRun({ status: 'queued' })).toBeNull();
		expect(normalizeRun(null)).toBeNull();
	});

	it('classifies pending and terminal statuses', () => {
		expect(isRunPending({ status: 'queued' })).toBe(true);
		expect(isRunPending({ status: 'running' })).toBe(true);
		expect(isRunPending({ status: 'succeeded' })).toBe(false);
		expect(isRunTerminal({ status: 'failed' })).toBe(true);
		expect(isRunTerminal({ status: 'running' })).toBe(false);
		expect(isRunPending(null)).toBe(false);
	});

	it('rejects a sleep with an AbortError when aborted', async () => {
		const controller = new AbortController();
		const pending = sleep(60_000, controller.signal);
		controller.abort();
		await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
		await expect(sleep(1, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
	});
});

describe('startOptimizationScan', () => {
	it('returns the queued run', async () => {
		api.requestOptimizationRun.mockResolvedValue({ run_id: 9, status: 'queued' });

		await expect(startOptimizationScan()).resolves.toEqual({
			supported: true,
			run: { run_id: 9, status: 'queued' }
		});
		expect(optimizationRunsSupported()).toBe(true);
	});

	it.each([404, 405])('detects a server without the runs API (%s) and stops asking', async (status) => {
		api.requestOptimizationRun.mockRejectedValue(apiError(status));

		await expect(startOptimizationScan()).resolves.toEqual({ supported: false, run: null });
		await expect(startOptimizationScan()).resolves.toEqual({ supported: false, run: null });

		expect(api.requestOptimizationRun).toHaveBeenCalledTimes(1);
		expect(optimizationRunsSupported()).toBe(false);
	});

	it('propagates other failures', async () => {
		api.requestOptimizationRun.mockRejectedValue(apiError(503, 'Scan queue unavailable'));
		await expect(startOptimizationScan()).rejects.toThrow('Scan queue unavailable');
	});
});

describe('findPendingOptimizationRun', () => {
	it('returns the latest run while it is pending', async () => {
		api.listOptimizationRuns.mockResolvedValue([{ run_id: 3, status: 'running' }]);

		await expect(findPendingOptimizationRun()).resolves.toMatchObject({ run_id: 3, status: 'running' });
		expect(api.listOptimizationRuns).toHaveBeenCalledWith(expect.objectContaining({ limit: 1 }));
	});

	it('ignores a completed latest run', async () => {
		api.listOptimizationRuns.mockResolvedValue([{ run_id: 3, status: 'succeeded' }]);
		await expect(findPendingOptimizationRun()).resolves.toBeNull();
	});

	it.each([404, 405])('returns null without throwing when the list endpoint is missing (%s)', async (status) => {
		api.listOptimizationRuns.mockRejectedValue(apiError(status));

		await expect(findPendingOptimizationRun()).resolves.toBeNull();
		await expect(findPendingOptimizationRun()).resolves.toBeNull();
		expect(api.listOptimizationRuns).toHaveBeenCalledTimes(1);
		expect(optimizationRunListSupported()).toBe(false);
	});

	it.each([404, 405])(
		'keeps requesting scans when only the optional list endpoint is missing (%s)',
		async (status) => {
			// E1 defines POST /optimization/runs and GET /optimization/runs/{id} only:
			// a missing list must not disable the Rescan button.
			api.listOptimizationRuns.mockRejectedValue(apiError(status));
			api.requestOptimizationRun.mockResolvedValue({ run_id: 11, status: 'queued' });

			await expect(findPendingOptimizationRun()).resolves.toBeNull();
			expect(optimizationRunsSupported()).toBeNull();

			await expect(startOptimizationScan()).resolves.toEqual({
				supported: true,
				run: { run_id: 11, status: 'queued' }
			});
			expect(api.requestOptimizationRun).toHaveBeenCalledTimes(1);
			expect(optimizationRunsSupported()).toBe(true);
		}
	);

	it('does not probe the list once the runs API is known to be missing', async () => {
		api.requestOptimizationRun.mockRejectedValue(apiError(404));
		await startOptimizationScan();

		await expect(findPendingOptimizationRun()).resolves.toBeNull();
		expect(api.listOptimizationRuns).not.toHaveBeenCalled();
	});

	it('returns null on transient list errors and probes again later', async () => {
		api.listOptimizationRuns.mockRejectedValueOnce(apiError(503));
		await expect(findPendingOptimizationRun()).resolves.toBeNull();
		expect(optimizationRunListSupported()).toBeNull();

		api.listOptimizationRuns.mockResolvedValueOnce({ items: [] });
		await expect(findPendingOptimizationRun()).resolves.toBeNull();
		expect(api.listOptimizationRuns).toHaveBeenCalledTimes(2);
	});
});

describe('waitForOptimizationRun', () => {
	it('polls with backoff until the run succeeds and reports each update', async () => {
		vi.useFakeTimers();
		api.getOptimizationRun
			.mockResolvedValueOnce({ run_id: 7, status: 'running' })
			.mockResolvedValueOnce({ run_id: 7, status: 'running' })
			.mockResolvedValueOnce({ run_id: 7, status: 'succeeded', recommendation_count: 4 });
		const updates = [];

		const pending = waitForOptimizationRun(
			{ run_id: 7, status: 'queued' },
			{ intervalMs: 1000, onUpdate: (run) => updates.push(run.status) }
		);
		await vi.advanceTimersByTimeAsync(999);
		expect(api.getOptimizationRun).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(api.getOptimizationRun).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(1500);
		expect(api.getOptimizationRun).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(2250);

		await expect(pending).resolves.toMatchObject({ run_id: 7, status: 'succeeded', recommendation_count: 4 });
		expect(updates).toEqual(['running', 'running', 'succeeded']);
		expect(api.getOptimizationRun).toHaveBeenCalledWith(7, expect.any(Object));
	});

	it('returns immediately for a run that already finished', async () => {
		await expect(waitForOptimizationRun({ run_id: 2, status: 'failed', error: 'boom' })).resolves.toMatchObject({
			status: 'failed'
		});
		expect(api.getOptimizationRun).not.toHaveBeenCalled();
	});

	it('flags a run still pending after the timeout', async () => {
		vi.useFakeTimers();
		api.getOptimizationRun.mockResolvedValue({ run_id: 7, status: 'running' });

		const pending = waitForOptimizationRun(7, { intervalMs: 1000, maxIntervalMs: 1000, timeoutMs: 3000 });
		await vi.advanceTimersByTimeAsync(5000);

		await expect(pending).resolves.toMatchObject({ run_id: 7, status: 'running', timedOut: true });
	});

	it('stops when the server no longer knows the run', async () => {
		vi.useFakeTimers();
		api.getOptimizationRun.mockRejectedValue(apiError(404));

		const pending = waitForOptimizationRun(7, { intervalMs: 10 });
		await vi.advanceTimersByTimeAsync(10);

		await expect(pending).resolves.toMatchObject({ run_id: 7, status: 'unknown' });
	});

	it('retries transient errors, then gives up', async () => {
		vi.useFakeTimers();
		api.getOptimizationRun
			.mockRejectedValueOnce(apiError(502))
			.mockResolvedValueOnce({ run_id: 7, status: 'succeeded' });

		const recovered = waitForOptimizationRun(7, { intervalMs: 10, maxIntervalMs: 10 });
		await vi.advanceTimersByTimeAsync(20);
		await expect(recovered).resolves.toMatchObject({ status: 'succeeded' });

		api.getOptimizationRun.mockReset();
		api.getOptimizationRun.mockRejectedValue(apiError(502, 'Bad gateway'));
		const failing = waitForOptimizationRun(8, { intervalMs: 10, maxIntervalMs: 10 });
		const assertion = expect(failing).rejects.toThrow('Bad gateway');
		await vi.advanceTimersByTimeAsync(100);
		await assertion;
		expect(api.getOptimizationRun).toHaveBeenCalledTimes(5);
	});

	it('rejects with an AbortError when the page goes away', async () => {
		vi.useFakeTimers();
		api.getOptimizationRun.mockResolvedValue({ run_id: 7, status: 'running' });
		const controller = new AbortController();

		const pending = waitForOptimizationRun(7, { intervalMs: 1000, signal: controller.signal });
		const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
		await vi.advanceTimersByTimeAsync(1000);
		controller.abort();
		await assertion;
		expect(api.getOptimizationRun).toHaveBeenCalledTimes(1);
		expect(api.getOptimizationRun.mock.calls[0][1].signal).toBe(controller.signal);
	});
});
