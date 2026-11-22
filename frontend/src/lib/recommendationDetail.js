/**
 * Presentation of one recommendation on its detail page (K3): projected
 * impact components (Impact Model), review evidence, consensus across
 * sources (Recommendation v2, shown only when the server returns it) and
 * measured outcome. Projected and measured values stay in separate blocks.
 */
import { currencyOrDefault } from '#lib/utils/format.js';

function finite(value) {
	const number = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
	return typeof number === 'number' && Number.isFinite(number) ? number : null;
}

const COST_COMPONENTS = [
	['cpu', 'CPU'],
	['ram', 'Memory'],
	['gpu', 'GPU'],
	['storage', 'Storage'],
	['network', 'Network'],
	['lb', 'Load balancer'],
	['other', 'Other (unsplit)']
];

const CO2E_COMPONENTS = {
	operational_energy: 'Operational energy',
	idle_share: 'Idle share',
	idle_operational: 'Idle share',
	dynamic_operational: 'Dynamic CPU',
	memory_operational: 'Memory power',
	gpu_operational: 'GPU power',
	embodied: 'Embodied (hardware)'
};

function componentRows(entries) {
	return entries.filter((row) => row.value !== null && row.value !== 0);
}

/**
 * Projected impact (`impact`, an ImpactDelta) as labelled rows; null when the
 * recommendation carries no Impact Model projection.
 */
export function impactOf(rec) {
	const impact = rec?.impact;
	if (!impact || typeof impact !== 'object') return null;
	const costBreakdown = impact.cost_breakdown_annual ?? {};
	const co2eComponents = impact.co2e_components_annual_g ?? impact.co2e_breakdown_annual_g ?? {};
	return {
		modelVersion: typeof impact.model_version === 'string' ? impact.model_version : null,
		kind: impact.kind ?? null,
		currency: currencyOrDefault(impact.currency),
		allocatedCostAnnual: finite(impact.allocated_cost_annual),
		expectedCostAnnual: finite(impact.expected_billable_cost_annual),
		realizationFactor: finite(impact.realization_factor),
		co2eAllocatedAnnualG: finite(impact.co2e_allocated_annual_g),
		co2eExpectedAnnualG: finite(impact.co2e_expected_annual_g),
		co2eMeasurableAnnualG: finite(impact.co2e_measurable_expected_annual_g),
		co2eModeled: impact.co2e_modeled !== false,
		carbonPrice: finite(impact.carbon_price_per_tonne),
		valueAnnual: finite(impact.value_annual),
		costComponents: componentRows(
			COST_COMPONENTS.map(([key, label]) => ({ key, label, value: finite(costBreakdown[key]) }))
		),
		co2eComponents: componentRows(
			Object.entries(co2eComponents)
				.filter(([key]) => key !== 'total')
				.map(([key, value]) => ({ key, label: CO2E_COMPONENTS[key] ?? key.replaceAll('_', ' '), value: finite(value) }))
		),
		assumptions:
			impact.assumptions && typeof impact.assumptions === 'object'
				? Object.entries(impact.assumptions).filter(([, value]) => value !== null && typeof value !== 'object')
				: []
	};
}

/** The expected-value block (`value`, I1) as display rows; null without one. */
export function valueOf(rec) {
	const value = rec?.value;
	if (!value || typeof value !== 'object') return null;
	return {
		currency: currencyOrDefault(value.currency),
		ev: finite(value.ev),
		valueAnnual: finite(value.value_annual),
		pSuccess: finite(value.p_success),
		incidentCost: finite(value.incident_cost),
		effortCost: finite(value.effort_cost),
		lane: typeof value.lane === 'string' ? value.lane : null
	};
}

/** Usage statistics rows (avg, p50, p95, p99, max) of the evidence block. */
function usage(stats) {
	if (!stats || typeof stats !== 'object') return null;
	return {
		avg: finite(stats.avg),
		p50: finite(stats.p50),
		p95: finite(stats.p95),
		p99: finite(stats.p99),
		max: finite(stats.max),
		samples: finite(stats.sample_count)
	};
}

/** Review evidence; null when the recommendation has none. */
export function evidenceOf(rec) {
	const evidence = rec?.evidence;
	if (!evidence || typeof evidence !== 'object') return null;
	return {
		windowStart: evidence.observation_window_start ?? null,
		windowEnd: evidence.observation_window_end ?? null,
		windowSeconds: finite(evidence.observation_window_seconds),
		samples: finite(evidence.sample_count),
		coverage: finite(evidence.coverage_ratio),
		cpu: usage(evidence.cpu_usage),
		memory: usage(evidence.memory_usage),
		restarts: finite(evidence.restart_count),
		oomEvents: finite(evidence.oom_events),
		savingsMethod: typeof evidence.savings_method === 'string' ? evidence.savings_method : null,
		changes: Array.isArray(evidence.changes) ? evidence.changes : [],
		riskFactors: Array.isArray(evidence.risk_factors) ? evidence.risk_factors : [],
		rollbackConditions: Array.isArray(evidence.rollback_conditions) ? evidence.rollback_conditions : [],
		diff: typeof evidence.proposed_diff === 'string' ? evidence.proposed_diff : '',
		generatedBy: typeof evidence.generated_by === 'string' ? evidence.generated_by : null
	};
}

/** A consensus value in its resource unit: `cpu_request_m` → "250m", `memory_request_bytes` → "512Mi". */
export function formatConsensusValue(resource, value) {
	const number = finite(value);
	if (number === null) return value === null || value === undefined ? '—' : String(value);
	const name = String(resource ?? '');
	if (name.startsWith('cpu')) return formatResource('cpu', number);
	if (name.startsWith('memory')) return formatResource('memory', number);
	return String(number);
}

function vote(item) {
	if (!item || typeof item !== 'object' || typeof item.source_id !== 'string') return null;
	return {
		sourceId: item.source_id,
		family: typeof item.family === 'string' ? item.family : null,
		value: item.value ?? null,
		direction: typeof item.direction === 'string' ? item.direction : null,
		counted: item.counted !== false,
		supportsChosen: item.supports_chosen === true
	};
}

/** Why a vote was not counted: another vote of its engine, or its push client already voted. */
export function uncountedReason(item, votes = []) {
	if (!item || item.counted) return '';
	const sameFamily = votes.some((other) => other !== item && other.family && other.family === item.family);
	if (sameFamily) return 'Same engine as another source, not counted';
	if (item.sourceId?.startsWith('push:')) return 'This push client already voted, not counted';
	return 'Not counted';
}

function strings(value) {
	return Array.isArray(value) ? value.filter((item) => typeof item === 'string' && item) : [];
}

/**
 * Consensus across sources (Recommendation v2, E6/E7): `{policy, resource,
 * value, chosen_from, agreement, contested, reasons, spread, independent_votes,
 * votes}`. Returns null for a v1 record so the page hides the section entirely.
 */
export function consensusOf(rec, v2 = null) {
	// GET /api/v2/recommendations/{id} first: the record the consensus engine wrote.
	const consensus = v2?.consensus && typeof v2.consensus === 'object' ? v2.consensus : rec?.consensus;
	if (!consensus || typeof consensus !== 'object') return null;
	const sourceIds = strings(consensus.source_ids).length ? strings(consensus.source_ids) : strings(v2?.source_ids);
	const spread = consensus.spread && typeof consensus.spread === 'object' ? consensus.spread : {};
	const resource = typeof consensus.resource === 'string' ? consensus.resource : null;
	const votes = Array.isArray(consensus.votes) ? consensus.votes : Array.isArray(rec.signals) ? rec.signals : [];
	return {
		policy: typeof consensus.policy === 'string' ? consensus.policy : null,
		resource,
		value: consensus.value ?? null,
		chosenFrom: Array.isArray(consensus.chosen_from) ? consensus.chosen_from.map(String) : [],
		agreement: finite(consensus.agreement),
		contested: consensus.contested === true,
		reasons: Array.isArray(consensus.reasons) ? consensus.reasons.filter((item) => typeof item === 'string') : [],
		independentVotes: finite(consensus.independent_votes),
		revision: finite(v2?.revision) ?? finite(rec?.revision),
		sourceIds,
		spread: Object.entries(spread)
			.filter(([, values]) => values && typeof values === 'object')
			.map(([name, values]) => ({
				resource: name,
				values: Object.entries(values).map(([source, value]) => ({
					source,
					value,
					display: formatConsensusValue(name, value)
				}))
			})),
		votes: votes.map(vote).filter(Boolean)
	};
}

/**
 * Revisions of a v2 recommendation (E7): the current one, newer advice queued
 * behind an open plan, and the history (newest first).
 */
export function revisionsOf(v2) {
	if (!v2 || typeof v2 !== 'object') return null;
	const history = (Array.isArray(v2.revisions) ? v2.revisions : [])
		.filter((item) => item && typeof item === 'object' && finite(item.revision) !== null)
		.map((item) => {
			const snapshot = item.snapshot && typeof item.snapshot === 'object' ? item.snapshot : {};
			return {
				revision: item.revision,
				state: typeof item.state === 'string' ? item.state : 'unknown',
				createdAt: item.created_at ?? null,
				cpuRequest: finite(snapshot.recommended_cpu_request_millicores),
				memoryRequest: finite(snapshot.recommended_memory_request_bytes)
			};
		});
	return {
		revision: finite(v2.revision),
		queuedRevision: finite(v2.queued_revision),
		queued: history.find((item) => item.state === 'queued') ?? null,
		history
	};
}

/** Measured outcome fields already on the record (legacy verifier, C0). */
export function measuredOf(rec) {
	const cost = finite(rec?.measured_cost_saved);
	const co2e = finite(rec?.measured_co2e_saved_grams);
	if (cost === null && co2e === null && !rec?.verification_status && !rec?.verified_at) return null;
	return {
		costAnnual: cost,
		co2eAnnualG: co2e,
		status: rec?.verification_status ?? null,
		realized: typeof rec?.savings_realized === 'boolean' ? rec.savings_realized : null,
		verifiedAt: rec?.verified_at ?? null,
		windowStart: rec?.verification_window_start ?? null,
		windowEnd: rec?.verification_window_end ?? null
	};
}

/** An http(s) URL safe to use as a link target, or null. */
export function safeHttpUrl(url) {
	if (typeof url !== 'string') return null;
	try {
		const parsed = new URL(url);
		return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
	} catch {
		return null;
	}
}

/** Lifecycle status words of a recommendation, with a tone for the pill. */
export const RECOMMENDATION_STATUS = {
	active: { label: 'Open', tone: 'yellow' },
	pr_open: { label: 'Pull request open', tone: 'blue' },
	applied: { label: 'Applied', tone: 'blue' },
	verifying: { label: 'Measuring', tone: 'blue' },
	verified: { label: 'Verified', tone: 'green' },
	ignored: { label: 'Ignored', tone: 'dark' },
	stale: { label: 'Stale', tone: 'dark' },
	expired: { label: 'Expired', tone: 'dark' },
	regressed: { label: 'Regressed', tone: 'red' },
	rollback_review: { label: 'Rollback review', tone: 'red' },
	reverted: { label: 'Reverted', tone: 'dark' },
	failed: { label: 'Failed', tone: 'red' }
};

export function recommendationStatus(status) {
	return RECOMMENDATION_STATUS[status] ?? { label: status ? String(status).replaceAll('_', ' ') : 'Unknown', tone: 'dark' };
}

/** Human label of a lifecycle event type. */
export function eventLabel(type) {
	const text = String(type ?? '').replaceAll('_', ' ');
	return text ? text[0].toUpperCase() + text.slice(1) : 'Event';
}

/** Short CPU (millicores) or memory (bytes) value, e.g. "250m" or "512Mi". */
export function formatResource(resource, value) {
	const number = finite(value);
	if (number === null) return '—';
	if (resource === 'memory') {
		const mi = number / (1024 * 1024);
		return mi >= 1024 ? `${(mi / 1024).toFixed(mi % 1024 ? 2 : 0)}Gi` : `${Math.round(mi)}Mi`;
	}
	if (resource === 'cpu') return `${Math.round(number)}m`;
	return String(number);
}

/** current → proposed request rows from the record's request fields. */
export function requestRows(rec) {
	const rows = [];
	if (rec?.current_cpu_request_millicores != null || rec?.recommended_cpu_request_millicores != null) {
		rows.push({
			resource: 'CPU request',
			current: formatResource('cpu', rec.current_cpu_request_millicores),
			proposed: formatResource('cpu', rec.recommended_cpu_request_millicores)
		});
	}
	if (rec?.current_memory_request_bytes != null || rec?.recommended_memory_request_bytes != null) {
		rows.push({
			resource: 'Memory request',
			current: formatResource('memory', rec.current_memory_request_bytes),
			proposed: formatResource('memory', rec.recommended_memory_request_bytes)
		});
	}
	return rows;
}
