/**
 * Applied changes and their measurements (`/api/v2/changes`, C2) for the
 * change and ledger pages that PR deep links open (J1):
 * `/changes/{plan_id}` and `/ledger?change={change_id or plan_id}`.
 */
import {
	ApiError,
	getAutomationPlanStatus,
	getChange,
	getOpenPullRequests,
	getRecommendationPullRequests,
	listChanges,
	listDeliveringRecommendations
} from '$lib/api.js';

/** Measurement states with a label; status words are always shown as text. */
export const CHANGE_STATES = {
	applied: { label: 'Applied, waiting for measurement', tone: 'blue' },
	measuring: { label: 'Measuring', tone: 'blue' },
	// "Realized" is reserved for billed savings (principle 3): a measurement outcome says
	// whether an allocated saving was measured.
	realized: { label: 'Saving measured', tone: 'green' },
	not_realized: { label: 'No saving measured', tone: 'yellow' },
	inconclusive: { label: 'Inconclusive', tone: 'dark' },
	contaminated: { label: 'Contaminated', tone: 'yellow' },
	regressed: { label: 'Regressed (rollback review)', tone: 'red' },
	closed: { label: 'Closed', tone: 'dark' }
};

export function stateLabel(state) {
	return CHANGE_STATES[state]?.label ?? (state ? String(state).replaceAll('_', ' ') : 'Not tracked yet');
}

export function stateTone(state) {
	return CHANGE_STATES[state]?.tone ?? 'dark';
}

/** Measured quantities with display names; `claimed` ones count as savings. */
export const QUANTITIES = {
	allocated_cost: { label: 'Allocated cost', kind: 'cost' },
	cost_cpu: { label: 'CPU cost', kind: 'cost' },
	cost_ram: { label: 'RAM cost', kind: 'cost' },
	co2e_g: { label: 'Operational CO₂e', kind: 'co2e' },
	core_hours: { label: 'Requested CPU', kind: 'unit' },
	gib_hours: { label: 'Requested memory', kind: 'unit' }
};

export function quantityLabel(quantity) {
	return QUANTITIES[quantity]?.label ?? String(quantity ?? '').replaceAll('_', ' ');
}

function isNotFound(error) {
	return error instanceof ApiError && (error.status === 404 || error.status === 422);
}

/**
 * Find the change a deep link points at: a change id, or a plan id whose
 * change the server filters (`GET /changes?plan_id=`, one request, J1).
 * @returns {Promise<Object|null>} the full change (`GET /changes/{id}`), or null
 */
export async function resolveChange(id, { signal, preferPlan = false } = {}) {
	if (!id) return null;
	const direct = async () => {
		try {
			return await getChange(id, { signal });
		} catch (error) {
			if (isNotFound(error)) return null;
			throw error;
		}
	};
	if (!preferPlan) {
		const change = await direct();
		if (change) return change;
	}
	let items = [];
	try {
		const result = await listChanges({ planId: id, limit: 1, signal });
		items = Array.isArray(result?.items) ? result.items : [];
	} catch (error) {
		// An id that cannot be a plan id is rejected with 422: it is not a plan.
		if (!isNotFound(error)) throw error;
	}
	// Never trust a server that ignored the filter: the change must name this plan.
	const match = items.find((item) => item?.plan_id === id);
	if (match) return getChange(match.change_id, { signal });
	return preferPlan ? direct() : null;
}

function finite(value) {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Measured estimates of one measurement, claimed (cost, CO₂e) first. */
export function measurementRows(measurement) {
	const estimates = Array.isArray(measurement?.estimates) ? measurement.estimates : [];
	return estimates
		.map((estimate) => ({
			quantity: estimate.quantity,
			label: quantityLabel(estimate.quantity),
			kind: QUANTITIES[estimate.quantity]?.kind ?? 'unit',
			unit: estimate.unit ?? '',
			claimed: estimate.claimed === true,
			annualized: finite(estimate.annualized),
			ciLow: finite(estimate.annualized_ci_low),
			ciHigh: finite(estimate.annualized_ci_high),
			projectedAnnual: finite(estimate.projected_annual),
			outcome: estimate.outcome ?? null,
			reason: estimate.reason ?? ''
		}))
		.sort((a, b) => Number(b.claimed) - Number(a.claimed));
}

/** The latest (current) measurement: the last one that nothing supersedes. */
export function latestMeasurement(items = []) {
	if (!Array.isArray(items) || !items.length) return null;
	const superseded = new Set(items.map((item) => item?.supersedes_measurement_id).filter(Boolean));
	const current = items.filter((item) => !superseded.has(item?.measurement_id));
	return (current.length ? current : items).at(-1);
}

/** "Deployment payments/api" for a change target. */
export function changeTarget(change) {
	const target = change?.target;
	if (!target) return 'Unknown workload';
	return `${target.kind ?? 'Workload'} ${target.namespace ?? ''}/${target.name ?? ''}`;
}

// --- Changes board (K4) ----------------------------------------------------
//
// One pipeline from approved plan to measured outcome. The rows merge three
// reads: applied changes (`GET /api/v2/changes`), pending and open pull
// requests (`GET /automation/pull-requests`) and, for recommendations whose
// pull request was merged but not applied yet (`GET /api/v2/recommendations
// ?status=pr_open`), their pull-request records. The plan of each row that
// has no applied change yet is read from the plans API for its exact status.

/** Board columns, in pipeline order. */
export const BOARD_STAGES = [
	{ id: 'plan', label: 'Plan', description: 'Approved; the pull request is being opened.' },
	{ id: 'pr_open', label: 'PR open', description: 'Waiting for review and merge.' },
	{ id: 'merged', label: 'Merged', description: 'Waiting for the GitOps controller to sync the merge.' },
	{ id: 'synced', label: 'Synced', description: 'Synced; waiting for the workload to roll out.' },
	{ id: 'applied', label: 'Applied', description: 'Rolled out; measurement starts after the warm-up.' },
	{ id: 'measuring', label: 'Measuring', description: 'The measurement window is open.' },
	{ id: 'outcome', label: 'Outcome', description: 'Saving measured, no saving measured, rollback review or closed.' }
];

const STAGE_IDS = new Set(BOARD_STAGES.map((stage) => stage.id));

/** Changes and lookups read per board load. */
export const BOARD_CHANGE_LIMIT = 200;
export const BOARD_LOOKUP_LIMIT = 25;

/** Plan statuses (`gitops.contracts.PlanStatus`) to board columns. */
const PLAN_STAGE = {
	draft: 'plan',
	validated: 'plan',
	approved: 'plan',
	applying: 'plan',
	pr_open: 'pr_open',
	merged: 'merged',
	sync_failed: 'merged',
	synced: 'synced',
	applied: 'applied',
	closed: 'outcome',
	stale: 'outcome',
	failed: 'outcome',
	needs_mapping: 'outcome',
	ambiguous: 'outcome',
	rejected: 'outcome',
	superseded: 'outcome',
	cancelled: 'outcome'
};

/** Words for plan and pull-request statuses that end a row without a change. */
const CLOSED_LABELS = {
	closed: 'Closed without merge',
	stale: 'Plan stale',
	failed: 'Plan failed',
	needs_mapping: 'Needs a source mapping',
	ambiguous: 'Ambiguous source',
	rejected: 'Plan rejected',
	superseded: 'Plan superseded',
	cancelled: 'Plan cancelled',
	error: 'Pull request failed'
};

/** Change measurement states to board columns. */
const CHANGE_STAGE = {
	applied: 'applied',
	measuring: 'measuring',
	realized: 'outcome',
	not_realized: 'outcome',
	inconclusive: 'outcome',
	contaminated: 'outcome',
	regressed: 'outcome',
	closed: 'outcome'
};

const SYNC_LABELS = {
	synced: 'Synced',
	pending: 'Sync pending',
	failed: 'Sync failed',
	unavailable: 'Sync unavailable',
	mismatched: 'Revision mismatch'
};

export function syncLabel(status) {
	if (typeof status !== 'string' || !status) return null;
	return SYNC_LABELS[status.toLowerCase()] ?? status.replaceAll('_', ' ');
}

function lower(value) {
	return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function timeOf(value) {
	const time = typeof value === 'string' ? Date.parse(value) : NaN;
	return Number.isNaN(time) ? 0 : time;
}

/** "Deployment/api" of a v2 recommendation target, a change target or nothing. */
function workloadOf(target) {
	if (!target || typeof target !== 'object' || !target.name) return null;
	return target.kind ? `${target.kind}/${target.name}` : String(target.name);
}

/** Pull-request stage when the plan is unknown (retention, plan API unavailable). */
function pullRequestStage(pr) {
	const status = lower(pr?.status);
	if (status === 'pending') return 'plan';
	if (status === 'open') return 'pr_open';
	if (status === 'merged') return lower(pr?.sync_status) === 'synced' ? 'synced' : 'merged';
	if (status === 'closed' || status === 'error') return 'outcome';
	return 'plan';
}

/**
 * Column of one board entry: the applied change decides once it exists,
 * then the plan status, then the pull-request record.
 * @returns {string} a `BOARD_STAGES` id
 */
export function boardStage({ change = null, plan = null, pr = null } = {}) {
	if (change) return CHANGE_STAGE[lower(change.state)] ?? 'applied';
	const planStage = PLAN_STAGE[lower(plan?.status)];
	if (planStage) return planStage;
	return pullRequestStage(pr);
}

/** What needs a human, as a sentence; null when the row is on track. */
export function boardAttention({ change = null, plan = null, pr = null } = {}) {
	if (change) return lower(change.state) === 'regressed' ? 'Regressed: review the rollback' : null;
	if (lower(plan?.status) === 'sync_failed' || lower(pr?.sync_status) === 'failed') {
		const code = typeof pr?.sync_error_code === 'string' ? ` (${pr.sync_error_code.replaceAll('_', ' ')})` : '';
		return `Sync failed${code}`;
	}
	if (lower(pr?.sync_status) === 'mismatched') return 'The controller synced another revision';
	if (lower(pr?.status) === 'error') return pr?.error ? `Pull request failed: ${pr.error}` : 'Pull request failed';
	return null;
}

function statusOf(stage, entry) {
	const { change, plan, pr } = entry;
	if (change) return { label: stateLabel(change.state ?? 'applied'), tone: stateTone(change.state ?? 'applied') };
	if (stage === 'outcome') {
		const key = CLOSED_LABELS[lower(plan?.status)] ? lower(plan?.status) : lower(pr?.status);
		return { label: CLOSED_LABELS[key] ?? 'Closed', tone: key === 'error' || key === 'failed' ? 'red' : 'dark' };
	}
	const column = BOARD_STAGES.find((item) => item.id === stage);
	return { label: column?.label ?? stage, tone: stage === 'merged' && boardAttention(entry) ? 'red' : 'blue' };
}

function updatedAt({ change, plan, pr }) {
	const candidates = [
		change?.state_updated_at,
		change?.rollout_completed_at,
		change?.created_at,
		pr?.sync_observed_at,
		pr?.merged_at,
		pr?.updated_at,
		pr?.created_at,
		plan?.created_at
	].filter((value) => typeof value === 'string' && timeOf(value) > 0);
	return candidates.sort((a, b) => timeOf(b) - timeOf(a))[0] ?? null;
}

/**
 * Merge changes, pull requests, plans and recommendations into board rows,
 * one per plan (or per change without a plan), newest first.
 * @param {{changes?: Object[], pullRequests?: Object[], plans?: Object, recommendations?: Object[]}} input
 */
export function buildChangeBoard({ changes = [], pullRequests = [], plans = {}, recommendations = [] } = {}) {
	const entries = new Map();
	const entry = (key) => {
		if (!entries.has(key)) entries.set(key, { change: null, pr: null, plan: null, planId: null });
		return entries.get(key);
	};
	(Array.isArray(changes) ? changes : []).forEach((change) => {
		if (!change?.change_id) return;
		const item = entry(change.plan_id ? `plan:${change.plan_id}` : `change:${change.change_id}`);
		// Changes arrive newest first: the first one of a plan is its current change.
		if (!item.change) item.change = change;
		item.planId = change.plan_id ?? item.planId;
	});
	(Array.isArray(pullRequests) ? pullRequests : []).forEach((pr, index) => {
		if (!pr || typeof pr !== 'object') return;
		const item = entry(pr.plan_id ? `plan:${pr.plan_id}` : `pr:${pr.id ?? index}`);
		if (!item.pr || timeOf(pr.updated_at ?? pr.created_at) > timeOf(item.pr.updated_at ?? item.pr.created_at)) item.pr = pr;
		item.planId = pr.plan_id ?? item.planId;
	});
	const planMap = plans && typeof plans === 'object' ? plans : {};
	const recs = new Map(
		(Array.isArray(recommendations) ? recommendations : []).filter((rec) => rec?.id != null).map((rec) => [Number(rec.id), rec])
	);

	const rows = [];
	for (const [key, item] of entries) {
		if (item.planId && planMap[item.planId]) item.plan = planMap[item.planId];
		const ids = new Set();
		(item.change?.recommendation_ids ?? []).forEach((id) => ids.add(Number(id)));
		(item.plan?.recommendation_ids ?? []).forEach((id) => ids.add(Number(id)));
		if (item.pr?.recommendation_id != null) ids.add(Number(item.pr.recommendation_id));
		const recommendationIds = [...ids].filter((id) => Number.isFinite(id)).sort((a, b) => a - b);
		const rec = recommendationIds.map((id) => recs.get(id)).find(Boolean);
		const target = item.change?.target ?? rec?.target ?? null;
		const stage = boardStage(item);
		const status = statusOf(stage, item);
		const changeId = item.change?.change_id ?? null;
		rows.push({
			key,
			stage,
			state: item.change?.state ?? null,
			statusLabel: status.label,
			tone: status.tone,
			attention: boardAttention(item),
			planId: item.planId,
			changeId,
			recommendationIds,
			workload: workloadOf(target) ?? (rec?.pod_name || null),
			namespace: target?.namespace ?? rec?.namespace ?? null,
			repo: item.pr?.repo ?? item.plan?.source?.repo ?? null,
			prUrl: safeUrl(item.pr?.pr_url ?? item.change?.pr_url),
			prNumber: item.pr?.pr_number ?? null,
			sync: syncLabel(item.pr?.sync_status),
			updatedAt: updatedAt(item),
			href: item.planId ? `/changes/${encodeURIComponent(item.planId)}` : null,
			ledgerHref: changeId ? `/ledger?change=${encodeURIComponent(changeId)}` : null
		});
	}
	return rows.sort((a, b) => timeOf(b.updatedAt) - timeOf(a.updatedAt));
}

function safeUrl(url) {
	if (typeof url !== 'string') return null;
	try {
		const parsed = new URL(url);
		return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
	} catch {
		return null;
	}
}

/** Board columns with their rows, in pipeline order. */
export function groupByStage(rows = []) {
	return BOARD_STAGES.map((stage) => ({ ...stage, rows: rows.filter((row) => row.stage === stage.id) }));
}

/** Namespaces present on the board, sorted. */
export function boardNamespaces(rows = []) {
	return [...new Set(rows.map((row) => row.namespace).filter(Boolean))].sort();
}

/**
 * Rows matching the board filters: a column, a namespace, rows that need
 * attention and free text (workload, namespace, repository, plan, change,
 * recommendation id).
 */
export function filterBoardRows(rows = [], { stage = '', namespace = '', attention = false, query = '' } = {}) {
	const text = lower(query);
	return rows.filter((row) => {
		if (stage && row.stage !== stage) return false;
		if (namespace && row.namespace !== namespace) return false;
		if (attention && !row.attention) return false;
		if (!text) return true;
		return [row.workload, row.namespace, row.repo, row.planId, row.changeId, ...row.recommendationIds.map((id) => `#${id}`)]
			.filter(Boolean)
			.some((value) => String(value).toLowerCase().includes(text));
	});
}

/** Board filters from the URL (`/changes?stage=measuring&namespace=shop&attention=1&q=api`). */
export function parseBoardFilters(searchParams) {
	const get = (key) => (searchParams && typeof searchParams.get === 'function' ? searchParams.get(key) ?? '' : '');
	const stage = get('stage');
	return {
		stage: STAGE_IDS.has(stage) ? stage : '',
		namespace: get('namespace'),
		attention: get('attention') === '1' || get('attention') === 'true',
		query: get('q')
	};
}

/** Query string of the board filters ("" when none is set). */
export function boardFilterQuery({ stage = '', namespace = '', attention = false, query = '' } = {}) {
	const params = new URLSearchParams();
	if (stage) params.set('stage', stage);
	if (namespace) params.set('namespace', namespace);
	if (attention) params.set('attention', '1');
	if (query) params.set('q', query);
	const text = params.toString();
	return text ? `?${text}` : '';
}

function failure(result, fallback) {
	if (result.status === 'fulfilled') return '';
	if (result.reason?.name === 'AbortError') return '';
	return result.reason?.message || fallback;
}

async function settleLimited(items, worker, limit) {
	const picked = items.slice(0, limit);
	const results = await Promise.allSettled(picked.map((item) => worker(item)));
	return { results: picked.map((item, index) => [item, results[index]]), skipped: Math.max(0, items.length - picked.length) };
}

/**
 * Read everything the board needs. A read that fails leaves its part empty
 * and is reported in `errors`; the board still shows what the others found.
 * @returns {Promise<{changes: Object[], nextCursor: string|null, pullRequests: Object[], plans: Object,
 *   recommendations: Object[], errors: {changes: string, pullRequests: string, recommendations: string},
 *   skippedLookups: number}|null>} null when aborted
 */
export async function loadChangeBoard({ signal, lookupLimit = BOARD_LOOKUP_LIMIT } = {}) {
	const [changesResult, prResult, recResult] = await Promise.allSettled([
		listChanges({ limit: BOARD_CHANGE_LIMIT, signal }),
		getOpenPullRequests({ signal }),
		listDeliveringRecommendations({ limit: BOARD_CHANGE_LIMIT, signal })
	]);
	if (signal?.aborted) return null;
	const changes = changesResult.status === 'fulfilled' && Array.isArray(changesResult.value?.items) ? changesResult.value.items : [];
	const nextCursor = changesResult.status === 'fulfilled' ? changesResult.value?.next_cursor ?? null : null;
	const openPrs = prResult.status === 'fulfilled' && Array.isArray(prResult.value) ? prResult.value : [];
	const recommendations = recResult.status === 'fulfilled' && Array.isArray(recResult.value?.items) ? recResult.value.items : [];
	const errors = {
		changes: failure(changesResult, 'Changes are temporarily unavailable.'),
		pullRequests: failure(prResult, 'Pull requests are unavailable.'),
		recommendations: failure(recResult, 'Recommendations in delivery are unavailable.')
	};

	// Merged pull requests leave the open list: find them through their recommendation.
	const covered = new Set();
	openPrs.forEach((pr) => pr?.recommendation_id != null && covered.add(Number(pr.recommendation_id)));
	changes.forEach((change) => (change?.recommendation_ids ?? []).forEach((id) => covered.add(Number(id))));
	const lookups = recommendations.map((rec) => Number(rec?.id)).filter((id) => Number.isFinite(id) && !covered.has(id));
	const merged = await settleLimited(lookups, (id) => getRecommendationPullRequests(id, { signal }), lookupLimit);
	if (signal?.aborted) return null;
	const pullRequests = [...openPrs];
	for (const [, result] of merged.results) {
		if (result.status !== 'fulfilled' || !Array.isArray(result.value)) continue;
		const current = result.value
			.filter((pr) => pr?.plan_id && ['merged', 'open', 'pending'].includes(lower(pr.status)))
			.sort((a, b) => timeOf(b.updated_at ?? b.created_at) - timeOf(a.updated_at ?? a.created_at))[0];
		if (current) pullRequests.push(current);
	}

	// Exact plan status for the rows that have no applied change yet.
	const changedPlans = new Set(changes.map((change) => change?.plan_id).filter(Boolean));
	const planIds = [...new Set(pullRequests.map((pr) => pr?.plan_id).filter((id) => id && !changedPlans.has(id)))];
	const planReads = await settleLimited(planIds, (id) => getAutomationPlanStatus(id, { signal }), lookupLimit);
	if (signal?.aborted) return null;
	const plans = {};
	for (const [id, result] of planReads.results) {
		if (result.status === 'fulfilled' && result.value && typeof result.value === 'object') plans[id] = result.value;
	}
	return {
		changes,
		nextCursor,
		pullRequests,
		plans,
		recommendations,
		errors,
		skippedLookups: merged.skipped + planReads.skipped
	};
}

/** The next page of older changes for "Load older changes". */
export async function loadMoreChanges(cursor, { signal } = {}) {
	const result = await listChanges({ limit: BOARD_CHANGE_LIMIT, cursor, signal });
	return {
		changes: Array.isArray(result?.items) ? result.items : [],
		nextCursor: result?.next_cursor ?? null
	};
}
