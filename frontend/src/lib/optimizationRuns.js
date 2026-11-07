/**
 * Recommendation scan runs (E1 / K9).
 *
 * Page loads only read persisted recommendations. A scan is requested
 * explicitly with POST /optimization/runs, then the run is polled until it
 * completes so the page can reload its results. Only a 404/405 from that POST
 * means the server has no runs API: callers then fall back to plain reads.
 *
 * Following a scan that is already running when a page opens uses the
 * optional list endpoint GET /optimization/runs?limit=N. Its absence is
 * cached separately and never disables requesting scans.
 */
import { getOptimizationRun, listOptimizationRuns, requestOptimizationRun } from '$lib/api.js';

export const RUN_PENDING_STATUSES = Object.freeze(['queued', 'running']);
export const RUN_TERMINAL_STATUSES = Object.freeze(['succeeded', 'failed']);

export const DEFAULT_POLL_INTERVAL_MS = 2000;
export const MAX_POLL_INTERVAL_MS = 10000;
export const DEFAULT_RUN_TIMEOUT_MS = 10 * 60 * 1000;
const MAX_CONSECUTIVE_POLL_ERRORS = 5;

/** null = not probed yet; false once POST /optimization/runs answered 404/405. */
let runsApiSupported = null;
/** null = not probed yet; false once GET /optimization/runs answered 404/405. */
let runsListSupported = null;

/** Whether the runs API is known to be available (null until probed). */
export function optimizationRunsSupported() {
	return runsApiSupported;
}

/** Whether the optional run list endpoint is known to be available (null until probed). */
export function optimizationRunListSupported() {
	return runsListSupported;
}

/** Forget the cached feature detection (tests, or after a server upgrade). */
export function resetOptimizationRunsSupport() {
	runsApiSupported = null;
	runsListSupported = null;
}

function isMissingRoute(error) {
	return error?.status === 404 || error?.status === 405;
}

function abortError() {
	try {
		return new DOMException('The operation was aborted.', 'AbortError');
	} catch {
		const error = new Error('The operation was aborted.');
		error.name = 'AbortError';
		return error;
	}
}

/** Normalize a run payload; returns null when it carries no run id. */
export function normalizeRun(run) {
	if (!run || typeof run !== 'object') return null;
	const runId = run.run_id ?? run.id;
	if (runId === null || runId === undefined || runId === '') return null;
	return {
		...run,
		run_id: runId,
		status: String(run.status ?? 'queued').toLowerCase()
	};
}

export function isRunPending(run) {
	return RUN_PENDING_STATUSES.includes(String(run?.status ?? '').toLowerCase());
}

export function isRunTerminal(run) {
	return RUN_TERMINAL_STATUSES.includes(String(run?.status ?? '').toLowerCase());
}

/** Resolve after `ms`, or reject with an AbortError when `signal` aborts. */
export function sleep(ms, signal) {
	return new Promise((resolve, reject) => {
		if (signal?.aborted) {
			reject(abortError());
			return;
		}
		const timer = setTimeout(() => {
			signal?.removeEventListener?.('abort', onAbort);
			resolve();
		}, ms);
		function onAbort() {
			clearTimeout(timer);
			reject(abortError());
		}
		signal?.addEventListener?.('abort', onAbort, { once: true });
	});
}

/**
 * Request a scan run.
 * @returns {Promise<{supported: boolean, run: Object|null}>}
 */
export async function startOptimizationScan({ signal } = {}) {
	if (runsApiSupported === false) return { supported: false, run: null };
	try {
		const run = normalizeRun(await requestOptimizationRun({ signal }));
		runsApiSupported = true;
		return { supported: true, run };
	} catch (error) {
		if (isMissingRoute(error)) {
			runsApiSupported = false;
			return { supported: false, run: null };
		}
		throw error;
	}
}

/**
 * Return the most recent run when it is still queued or running, so a page
 * opened during a scheduled scan reloads when that scan completes.
 *
 * The list endpoint is optional: E1 only requires POST /optimization/runs and
 * GET /optimization/runs/{id}. A 404/405 here is cached as "no list" and
 * leaves scan requests enabled. Never throws except on abort: an unavailable
 * list simply means "nothing to follow".
 */
export async function findPendingOptimizationRun({ signal } = {}) {
	if (runsApiSupported === false || runsListSupported === false) return null;
	try {
		const runs = await listOptimizationRuns({ limit: 1, signal });
		runsListSupported = true;
		const latest = normalizeRun(Array.isArray(runs) ? runs[0] : null);
		return latest && isRunPending(latest) ? latest : null;
	} catch (error) {
		if (error?.name === 'AbortError') throw error;
		if (isMissingRoute(error)) runsListSupported = false;
		return null;
	}
}

/**
 * Poll a run until it succeeds or fails.
 *
 * Resolves with the last known run; `timedOut: true` is set when the run is
 * still pending after `timeoutMs`, and `status: 'unknown'` when the server no
 * longer knows the run (404). Transient errors are retried with backoff.
 * Rejects with an AbortError when `signal` aborts.
 */
export async function waitForOptimizationRun(
	runOrId,
	{
		signal,
		onUpdate,
		intervalMs = DEFAULT_POLL_INTERVAL_MS,
		maxIntervalMs = MAX_POLL_INTERVAL_MS,
		timeoutMs = DEFAULT_RUN_TIMEOUT_MS,
		now = () => Date.now()
	} = {}
) {
	let current =
		typeof runOrId === 'object' ? normalizeRun(runOrId) : normalizeRun({ run_id: runOrId, status: 'queued' });
	if (!current) throw new Error('A run id is required to follow a recommendation scan.');
	if (isRunTerminal(current)) return current;

	const deadline = now() + timeoutMs;
	let delay = intervalMs;
	let consecutiveErrors = 0;

	while (now() < deadline) {
		await sleep(delay, signal);
		try {
			const next = normalizeRun(await getOptimizationRun(current.run_id, { signal }));
			consecutiveErrors = 0;
			if (next) {
				current = next;
				onUpdate?.(current);
			}
			if (isRunTerminal(current)) return current;
		} catch (error) {
			if (error?.name === 'AbortError') throw error;
			if (error?.status === 404) {
				current = { ...current, status: 'unknown' };
				onUpdate?.(current);
				return current;
			}
			if (error?.status === 401) throw error;
			consecutiveErrors += 1;
			if (consecutiveErrors >= MAX_CONSECUTIVE_POLL_ERRORS) throw error;
		}
		delay = Math.min(Math.round(delay * 1.5), maxIntervalMs);
	}
	return { ...current, timedOut: true };
}
