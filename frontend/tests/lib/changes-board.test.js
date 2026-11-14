/**
 * Changes board mapping (K4): plan, pull-request and change states to board
 * columns, merging, filters and the board loader.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => {
	class ApiError extends Error {
		constructor(message, status) {
			super(message);
			this.name = 'ApiError';
			this.status = status;
		}
	}
	return {
		ApiError,
		getChange: vi.fn(),
		listChanges: vi.fn(),
		getOpenPullRequests: vi.fn(),
		getRecommendationPullRequests: vi.fn(),
		listDeliveringRecommendations: vi.fn(),
		getAutomationPlanStatus: vi.fn()
	};
});

import * as api from '$lib/api.js';
import {
	BOARD_STAGES,
	boardAttention,
	boardFilterQuery,
	boardNamespaces,
	boardStage,
	buildChangeBoard,
	filterBoardRows,
	groupByStage,
	loadChangeBoard,
	loadMoreChanges,
	parseBoardFilters
} from '$lib/changes.js';

const ago = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString();

describe('boardStage', () => {
	it('orders the columns along the pipeline', () => {
		expect(BOARD_STAGES.map((stage) => stage.id)).toEqual(['plan', 'pr_open', 'merged', 'synced', 'applied', 'measuring', 'outcome']);
	});

	it.each([
		['approved', 'plan'],
		['applying', 'plan'],
		['pr_open', 'pr_open'],
		['merged', 'merged'],
		['sync_failed', 'merged'],
		['synced', 'synced'],
		['applied', 'applied'],
		['closed', 'outcome'],
		['superseded', 'outcome']
	])('maps plan status %s to %s', (status, stage) => {
		expect(boardStage({ plan: { status } })).toBe(stage);
	});

	it.each([
		[{ status: 'pending' }, 'plan'],
		[{ status: 'open', sync_status: null }, 'pr_open'],
		[{ status: 'merged', sync_status: 'pending' }, 'merged'],
		[{ status: 'merged', sync_status: 'synced' }, 'synced'],
		[{ status: 'closed' }, 'outcome'],
		[{ status: 'error' }, 'outcome']
	])('falls back on the pull request %o without a plan', (pr, stage) => {
		expect(boardStage({ pr })).toBe(stage);
	});

	it.each([
		['applied', 'applied'],
		['measuring', 'measuring'],
		['realized', 'outcome'],
		['not_realized', 'outcome'],
		['inconclusive', 'outcome'],
		['regressed', 'outcome'],
		[null, 'applied']
	])('lets the applied change (state %s) decide over plan and PR', (state, stage) => {
		expect(boardStage({ change: { state }, plan: { status: 'pr_open' }, pr: { status: 'open' } })).toBe(stage);
	});
});

describe('boardAttention', () => {
	it('flags failed syncs, revision mismatches, failed PRs and regressions in words', () => {
		expect(boardAttention({ plan: { status: 'sync_failed' }, pr: { sync_error_code: 'controller_not_found' } })).toBe(
			'Sync failed (controller not found)'
		);
		expect(boardAttention({ pr: { status: 'merged', sync_status: 'mismatched' } })).toBe('The controller synced another revision');
		expect(boardAttention({ pr: { status: 'error', error: 'token expired' } })).toBe('Pull request failed: token expired');
		expect(boardAttention({ change: { state: 'regressed' } })).toBe('Regressed: review the rollback');
		expect(boardAttention({ change: { state: 'measuring' }, pr: { sync_status: 'failed' } })).toBeNull();
		expect(boardAttention({ pr: { status: 'open' } })).toBeNull();
	});
});

describe('buildChangeBoard', () => {
	const changes = [
		{
			change_id: 'chg-2',
			plan_id: 'plan-2',
			state: 'measuring',
			target: { kind: 'Deployment', namespace: 'shop', name: 'api' },
			recommendation_ids: [12],
			pr_url: 'https://github.com/acme/gitops/pull/2',
			state_updated_at: ago(5)
		},
		{ change_id: 'chg-0', plan_id: null, state: 'realized', target: { kind: 'StatefulSet', namespace: 'db', name: 'pg' }, recommendation_ids: [], created_at: ago(600) }
	];
	const pullRequests = [
		{ id: 1, recommendation_id: 7, plan_id: 'plan-7', status: 'open', repo: 'acme/gitops', pr_number: 9, pr_url: 'https://github.com/acme/gitops/pull/9', created_at: ago(30) },
		{ id: 2, recommendation_id: 12, plan_id: 'plan-2', status: 'open', repo: 'acme/gitops', created_at: ago(90) },
		{ id: 3, recommendation_id: 8, plan_id: 'plan-8', status: 'merged', sync_status: 'failed', sync_error_code: 'controller_failed', repo: 'acme/gitops', updated_at: ago(10) },
		{ id: 4, recommendation_id: 9, plan_id: null, status: 'pending', pr_url: 'javascript:alert(1)', created_at: ago(1) }
	];
	const plans = { 'plan-7': { plan_id: 'plan-7', status: 'pr_open', recommendation_ids: [7, 70], source: { repo: 'acme/gitops' } } };
	const recommendations = [
		{ id: 7, target: { kind: 'Deployment', namespace: 'payments', name: 'checkout' } },
		{ id: 8, target: { kind: 'Deployment', namespace: 'shop', name: 'web' } }
	];

	const rows = buildChangeBoard({ changes, pullRequests, plans, recommendations });
	const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));

	it('merges one row per plan; the applied change wins over its open PR record', () => {
		expect(rows.map((row) => row.key)).toEqual(['pr:4', 'plan:plan-2', 'plan:plan-8', 'plan:plan-7', 'change:chg-0']);
		expect(byKey['plan:plan-2']).toMatchObject({
			stage: 'measuring',
			statusLabel: 'Measuring',
			changeId: 'chg-2',
			workload: 'Deployment/api',
			namespace: 'shop',
			href: '/changes/plan-2',
			ledgerHref: '/ledger?change=chg-2',
			prUrl: 'https://github.com/acme/gitops/pull/2'
		});
	});

	it('names PR rows after their recommendation target and links plan, PR and recommendations', () => {
		expect(byKey['plan:plan-7']).toMatchObject({
			stage: 'pr_open',
			workload: 'Deployment/checkout',
			namespace: 'payments',
			recommendationIds: [7, 70],
			prNumber: 9,
			ledgerHref: null,
			href: '/changes/plan-7'
		});
	});

	it('shows a failed sync on a merged PR as needing attention', () => {
		expect(byKey['plan:plan-8']).toMatchObject({ stage: 'merged', attention: 'Sync failed (controller failed)', sync: 'Sync failed', tone: 'red' });
	});

	it('keeps changes without a plan and drops unsafe PR links', () => {
		expect(byKey['change:chg-0']).toMatchObject({ stage: 'outcome', statusLabel: 'Saving measured', href: null, ledgerHref: '/ledger?change=chg-0' });
		expect(byKey['pr:4']).toMatchObject({ stage: 'plan', prUrl: null, href: null });
	});

	it('groups rows by column and lists the namespaces', () => {
		const columns = groupByStage(rows);
		expect(columns.map((column) => [column.id, column.rows.length])).toEqual([
			['plan', 1],
			['pr_open', 1],
			['merged', 1],
			['synced', 0],
			['applied', 0],
			['measuring', 1],
			['outcome', 1]
		]);
		expect(boardNamespaces(rows)).toEqual(['db', 'payments', 'shop']);
	});

	it('filters by stage, namespace, attention and free text', () => {
		expect(filterBoardRows(rows, { stage: 'measuring' }).map((row) => row.key)).toEqual(['plan:plan-2']);
		expect(filterBoardRows(rows, { namespace: 'shop' }).map((row) => row.key)).toEqual(['plan:plan-2', 'plan:plan-8']);
		expect(filterBoardRows(rows, { attention: true }).map((row) => row.key)).toEqual(['plan:plan-8']);
		expect(filterBoardRows(rows, { query: 'CHECKOUT' }).map((row) => row.key)).toEqual(['plan:plan-7']);
		expect(filterBoardRows(rows, { query: '#70' }).map((row) => row.key)).toEqual(['plan:plan-7']);
		expect(filterBoardRows(rows, {})).toHaveLength(rows.length);
	});

	it('closes rows of plans that ended without a change', () => {
		const [row] = buildChangeBoard({
			pullRequests: [{ id: 5, plan_id: 'plan-5', status: 'open' }],
			plans: { 'plan-5': { status: 'superseded', recommendation_ids: [] } }
		});
		expect(row).toMatchObject({ stage: 'outcome', statusLabel: 'Plan superseded' });
	});

	it('survives missing and malformed inputs', () => {
		expect(buildChangeBoard()).toEqual([]);
		expect(buildChangeBoard({ changes: null, pullRequests: [null, 'x'], plans: null, recommendations: null })).toEqual([]);
	});
});

describe('board filters in the URL', () => {
	it('round-trips the filters and ignores unknown stages', () => {
		const filters = { stage: 'measuring', namespace: 'shop', attention: true, query: 'api' };
		const query = boardFilterQuery(filters);
		expect(query).toBe('?stage=measuring&namespace=shop&attention=1&q=api');
		expect(parseBoardFilters(new URLSearchParams(query))).toEqual(filters);
		expect(parseBoardFilters(new URLSearchParams('stage=deleted'))).toMatchObject({ stage: '' });
		expect(parseBoardFilters(undefined)).toEqual({ stage: '', namespace: '', attention: false, query: '' });
		expect(boardFilterQuery({})).toBe('');
	});
});

describe('loadChangeBoard', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		api.listChanges.mockResolvedValue({
			items: [{ change_id: 'chg-1', plan_id: 'plan-1', state: 'applied', recommendation_ids: [1] }],
			next_cursor: 'cursor-1'
		});
		api.getOpenPullRequests.mockResolvedValue([{ id: 1, recommendation_id: 2, plan_id: 'plan-2', status: 'open' }]);
		api.listDeliveringRecommendations.mockResolvedValue({ items: [{ id: 1 }, { id: 2 }, { id: 3 }], total: 3 });
		api.getRecommendationPullRequests.mockResolvedValue([
			{ id: 30, recommendation_id: 3, plan_id: 'plan-3-old', status: 'closed', created_at: ago(100) },
			{ id: 31, recommendation_id: 3, plan_id: 'plan-3', status: 'merged', created_at: ago(50) }
		]);
		api.getAutomationPlanStatus.mockImplementation(async (id) => ({ plan_id: id, status: id === 'plan-3' ? 'merged' : 'pr_open', recommendation_ids: [] }));
	});

	it('finds merged pull requests through recommendations still in delivery and reads their plans', async () => {
		const result = await loadChangeBoard();

		expect(api.listChanges).toHaveBeenCalledWith(expect.objectContaining({ limit: 200 }));
		expect(api.listDeliveringRecommendations).toHaveBeenCalledWith(expect.objectContaining({ limit: 200 }));
		// 1 has a change and 2 an open PR: only 3 needs its PR history.
		expect(api.getRecommendationPullRequests).toHaveBeenCalledTimes(1);
		expect(api.getRecommendationPullRequests).toHaveBeenCalledWith(3, expect.anything());
		// Plans are read for rows without a change only.
		expect(api.getAutomationPlanStatus.mock.calls.map(([id]) => id).sort()).toEqual(['plan-2', 'plan-3']);
		expect(result.pullRequests.map((pr) => pr.id)).toEqual([1, 31]);
		expect(result.nextCursor).toBe('cursor-1');
		expect(result.errors).toEqual({ changes: '', pullRequests: '', recommendations: '' });

		const stages = Object.fromEntries(buildChangeBoard(result).map((row) => [row.planId, row.stage]));
		expect(stages).toEqual({ 'plan-1': 'applied', 'plan-2': 'pr_open', 'plan-3': 'merged' });
	});

	it('reports a failed read and still returns the others', async () => {
		api.getOpenPullRequests.mockRejectedValue(new Error('Pull requests are down.'));
		api.getAutomationPlanStatus.mockRejectedValue(new api.ApiError('Server-side automation plan service is not configured.', 503));

		const result = await loadChangeBoard();

		expect(result.errors.pullRequests).toBe('Pull requests are down.');
		expect(result.changes).toHaveLength(1);
		expect(result.plans).toEqual({});
		// Without its plan, the merged PR record still places the row.
		expect(buildChangeBoard(result).find((row) => row.planId === 'plan-3').stage).toBe('merged');
	});

	it('caps the per-row lookups and says how many were skipped', async () => {
		api.listDeliveringRecommendations.mockResolvedValue({ items: [{ id: 3 }, { id: 4 }, { id: 5 }] });
		const result = await loadChangeBoard({ lookupLimit: 1 });
		expect(api.getRecommendationPullRequests).toHaveBeenCalledTimes(1);
		expect(result.skippedLookups).toBe(2 + 1);
	});

	it('returns null once aborted', async () => {
		const controller = new AbortController();
		controller.abort();
		await expect(loadChangeBoard({ signal: controller.signal })).resolves.toBeNull();
	});

	it('pages older changes with the cursor', async () => {
		api.listChanges.mockResolvedValue({ items: [{ change_id: 'old' }], next_cursor: null });
		await expect(loadMoreChanges('cursor-1')).resolves.toEqual({ changes: [{ change_id: 'old' }], nextCursor: null });
		expect(api.listChanges).toHaveBeenCalledWith(expect.objectContaining({ cursor: 'cursor-1', limit: 200 }));
	});
});
