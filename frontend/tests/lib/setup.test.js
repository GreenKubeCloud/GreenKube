/**
 * Setup checklist (F1) and repository suggestions (F2) normalization.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	SETUP_BANNER_DISMISSED_KEY,
	allowLabel,
	groupChecks,
	isCapabilityActive,
	normalizeCheck,
	normalizeSetupStatus,
	normalizeSuggestions,
	normalizeUiAction,
	setupBannerDismissed
} from '$lib/setup.js';
import { createMemoryStorage, installStorage } from '../helpers/storage.js';

export const SETUP_STATUS = {
	complete: false,
	checks: [
		{
			id: 'prometheus',
			category: 'data_sources',
			title: 'Prometheus',
			status: 'ok',
			reason: 'Reachable at http://prometheus:9090',
			fix: { text: '' },
			docs_url: 'https://greenkube.cloud/docs/prometheus'
		},
		{
			id: 'git-credentials',
			category: 'credentials',
			title: 'Git credentials',
			status: 'action_needed',
			reason: 'No credentials for github.com',
			fix: {
				text: 'Create the greenkube-git-credentials Secret.',
				snippet: 'kubectl create secret generic greenkube-git-credentials --from-literal=token=…'
			},
			docs_url: 'javascript:alert(1)'
		},
		{
			id: 'repos',
			category: 'repositories',
			title: 'Repository allowlist',
			status: 'blocked',
			reason: '3 repositories are not allowlisted',
			fix: { text: 'Allow the discovered repositories.', ui_action: 'allowlist_repositories' },
			docs_url: '/docs/automation'
		},
		{ id: 'argo', category: 'gitops', title: 'Argo CD', status: 'weird', reason: '', fix: null }
	],
	capabilities: {
		discovery: { state: 'active', reason: 'Argo CD found' },
		delivery: { state: 'dormant', reason: 'Waiting for credentials' },
		sync_tracking: { state: 'active', reason: '' },
		measurement: { state: 'warming_up', reason: 'Collecting baseline' }
	}
};

describe('normalizeSetupStatus', () => {
	it('maps the contract and counts the checks that need action', () => {
		const status = normalizeSetupStatus(SETUP_STATUS);
		expect(status.complete).toBe(false);
		expect(status.actionCount).toBe(2);
		expect(status.capabilities.map((cap) => [cap.id, cap.state])).toEqual([
			['discovery', 'active'],
			['delivery', 'dormant'],
			['sync_tracking', 'active'],
			['measurement', 'warming_up']
		]);
	});

	it('is null (unknown) for a malformed or empty payload', () => {
		expect(normalizeSetupStatus(null)).toBeNull();
		expect(normalizeSetupStatus({})).toBeNull();
		expect(normalizeSetupStatus({ complete: 'no' })).toBeNull();
	});

	it('reads an unknown check status as pending, never as ok', () => {
		expect(normalizeCheck({ id: 'x', status: 'weird' }).status).toBe('pending');
	});

	it('keeps only https or in-app documentation links', () => {
		const [ok, bad, local] = SETUP_STATUS.checks.map(normalizeCheck);
		expect(ok.docsUrl).toBe('https://greenkube.cloud/docs/prometheus');
		expect(bad.docsUrl).toBe('');
		expect(local.docsUrl).toBe('/docs/automation');
	});
});

describe('groupChecks', () => {
	it('groups by category in the contract order and puts unknown categories last', () => {
		const checks = [...SETUP_STATUS.checks, { id: 'z', category: 'mystery', status: 'ok' }].map(normalizeCheck);
		expect(groupChecks(checks).map((group) => group.id)).toEqual([
			'data_sources',
			'gitops',
			'repositories',
			'credentials',
			'other'
		]);
	});
});

describe('normalizeUiAction', () => {
	it('routes known actions and in-app paths, never other origins', () => {
		expect(normalizeUiAction('allowlist_repositories').href).toBe('/setup#repository-suggestions');
		expect(normalizeUiAction('/automation')).toEqual({ label: 'Open', href: '/automation' });
		expect(normalizeUiAction('//evil.example/x')).toEqual({ label: '//evil.example/x', href: null });
		expect(normalizeUiAction({ label: 'Go', href: 'https://evil.example' })).toEqual({ label: 'Go', href: null });
		expect(normalizeUiAction({ label: 'Keys', href: '/settings/api-keys' })).toEqual({ label: 'Keys', href: '/settings/api-keys' });
		expect(normalizeUiAction(null)).toBeNull();
	});
});

describe('suggestions', () => {
	it('normalizes the F2 contract and drops entries without a URL', () => {
		const list = normalizeSuggestions({
			suggestions: [
				{ repo_url: 'https://github.com/acme/apps', provider_type: 'github', api_base_url: 'https://api.github.com', inferred_from: 'argocd/payments', allowlisted: false },
				{ repo_url: '', provider_type: 'gitlab' },
				{ repo_url: 'https://gitea.local/acme/infra', allowlisted: true }
			]
		});
		expect(list).toEqual([
			{ repoUrl: 'https://github.com/acme/apps', providerType: 'github', apiBaseUrl: 'https://api.github.com', inferredFrom: 'argocd/payments', allowlisted: false },
			{ repoUrl: 'https://gitea.local/acme/infra', providerType: 'unknown', apiBaseUrl: '', inferredFrom: '', allowlisted: true }
		]);
		expect(normalizeSuggestions(undefined)).toEqual([]);
	});

	it('labels the call to action', () => {
		expect(allowLabel(3)).toBe('Allow these 3 repositories');
		expect(allowLabel(1)).toBe('Allow this repository');
	});

	it('treats active-like capability states as active', () => {
		expect(isCapabilityActive('active')).toBe(true);
		expect(isCapabilityActive('dormant')).toBe(false);
	});
});

describe('setupBannerDismissed', () => {
	let storage;
	let restore;

	beforeEach(() => {
		storage = createMemoryStorage();
		restore = installStorage('sessionStorage', storage);
		setupBannerDismissed.set(false);
	});

	afterEach(() => restore());

	it('persists the dismissal for the tab session', () => {
		setupBannerDismissed.set(true);
		expect(get(setupBannerDismissed)).toBe(true);
		expect(storage.getItem(SETUP_BANNER_DISMISSED_KEY)).toBe('true');
		setupBannerDismissed.set(false);
		expect(storage.getItem(SETUP_BANNER_DISMISSED_KEY)).toBeNull();
	});

	it('keeps working in memory when storage is blocked', () => {
		restore();
		restore = installStorage('sessionStorage', createMemoryStorage({ failing: true }));
		setupBannerDismissed.set(true);
		expect(get(setupBannerDismissed)).toBe(true);
	});
});
