/**
 * Recommendation sources (E5) as shown on the Sources page (K6): status
 * words, run summaries and the measured track record of each source family.
 */

/** Source statuses from `GET /sources` with a label and tone; never colour-only. */
export const SOURCE_STATUS = {
	healthy: { label: 'Healthy', tone: 'green', icon: '✓' },
	degraded: { label: 'Degraded', tone: 'yellow', icon: '!' },
	failing: { label: 'Failing', tone: 'red', icon: '✕' },
	unavailable: { label: 'Unavailable', tone: 'dark', icon: '–' },
	disabled: { label: 'Disabled', tone: 'dark', icon: '–' },
	misconfigured: { label: 'Misconfigured', tone: 'red', icon: '✕' },
	pending: { label: 'Not run yet', tone: 'blue', icon: '…' },
	unregistered: { label: 'Unregistered', tone: 'dark', icon: '?' }
};

export function sourceStatus(status) {
	return SOURCE_STATUS[status] ?? { label: status ? String(status) : 'Unknown', tone: 'dark', icon: '?' };
}

/** Run statuses of `GET /sources/{id}/runs`. */
export function runTone(status) {
	return { succeeded: 'green', incomplete: 'yellow', skipped: 'yellow', failed: 'red', unavailable: 'dark' }[status] ?? 'dark';
}

/** Order: problems first (failing, misconfigured, degraded), then the server order. */
export function sortSources(sources = []) {
	const rank = { failing: 0, misconfigured: 0, degraded: 1 };
	return [...sources]
		.map((source, index) => ({ source, index }))
		.sort((a, b) => (rank[a.source.status] ?? 2) - (rank[b.source.status] ?? 2) || a.index - b.index)
		.map(({ source }) => source);
}

/**
 * Track-record entries of a source: matched by its v1 family
 * (`legacy_source`) or its id. Returns `[{capability, summary, mean, decided}]`.
 */
export function trackRecordFor(source, entries = []) {
	const families = new Set([source?.legacy_source, source?.id].filter(Boolean).map(String));
	return (Array.isArray(entries) ? entries : [])
		.filter((entry) => families.has(String(entry?.source_family)))
		.map((entry) => ({
			capability: entry.capability ?? 'all',
			summary: typeof entry.summary === 'string' ? entry.summary : '',
			mean: typeof entry.mean === 'number' ? entry.mean : null,
			decided: typeof entry.decided === 'number' ? entry.decided : 0
		}));
}
