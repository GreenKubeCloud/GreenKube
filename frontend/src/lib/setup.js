/**
 * Guided setup checklist (F1, K6) and repository allowlist suggestions (F2).
 *
 * The server owns every decision: `GET /setup/status` says which checks pass,
 * why the others do not and how to fix them. These helpers only normalize the
 * payload defensively (an older server answers 404, a newer one may add
 * fields) and keep the banner dismissal for the current browser tab.
 */
import { get, writable } from 'svelte/store';

/** Display order and labels of the check categories (the F1 contract). */
export const SETUP_CATEGORIES = [
	{ id: 'data_sources', label: 'Data sources' },
	{ id: 'recommendation_sources', label: 'Recommendation sources' },
	{ id: 'gitops', label: 'GitOps controllers' },
	{ id: 'repositories', label: 'Repositories' },
	{ id: 'credentials', label: 'Git credentials' },
	{ id: 'renderer', label: 'Renderer' },
	{ id: 'executor', label: 'Plan executor' },
	{ id: 'evidence', label: 'Evidence warm-up' }
];

/** Check statuses with a text label and icon, so status is never colour-only. */
export const CHECK_STATUS = {
	ok: { label: 'OK', icon: '✓', tone: 'green' },
	action_needed: { label: 'Action needed', icon: '!', tone: 'yellow' },
	blocked: { label: 'Blocked', icon: '✕', tone: 'red' },
	pending: { label: 'Pending', icon: '…', tone: 'blue' },
	not_applicable: { label: 'Not applicable', icon: '–', tone: 'dark' }
};

export const CAPABILITIES = [
	{ id: 'discovery', label: 'Discovery' },
	{ id: 'delivery', label: 'Delivery (pull requests)' },
	{ id: 'sync_tracking', label: 'Sync tracking' },
	{ id: 'measurement', label: 'Measurement' }
];

function text(value) {
	return typeof value === 'string' ? value : '';
}

function normalizeFix(fix) {
	if (typeof fix === 'string') return { text: fix, snippet: '', uiAction: null };
	if (!fix || typeof fix !== 'object') return { text: '', snippet: '', uiAction: null };
	return { text: text(fix.text), snippet: text(fix.snippet), uiAction: normalizeUiAction(fix.ui_action) };
}

/** Route a few well-known UI actions to the place on this app that performs them. */
const UI_ACTIONS = {
	allowlist_repositories: { label: 'Review repository suggestions', href: '/setup#repository-suggestions' },
	allow_repositories: { label: 'Review repository suggestions', href: '/setup#repository-suggestions' },
	resolve_bindings: { label: 'Resolve workload bindings', href: '/automation' },
	sign_in: { label: 'Account settings', href: '/settings/account' },
	create_api_key: { label: 'Create an API key', href: '/settings/api-keys' },
	view_sources: { label: 'Open recommendation sources', href: '/sources' }
};

/**
 * `ui_action` is optional: a known action id, an in-app path, or
 * `{label, href}`. Anything else is shown as plain text, never as a link to
 * an arbitrary origin.
 */
export function normalizeUiAction(action) {
	if (!action) return null;
	if (typeof action === 'string') {
		if (UI_ACTIONS[action]) return UI_ACTIONS[action];
		if (action.startsWith('/') && !action.startsWith('//')) return { label: 'Open', href: action };
		return { label: action, href: null };
	}
	if (typeof action === 'object') {
		const href = text(action.href) || text(action.path);
		const safe = href.startsWith('/') && !href.startsWith('//') ? href : null;
		const known = UI_ACTIONS[text(action.id) || text(action.action)];
		return {
			label: text(action.label) || known?.label || 'Open',
			href: safe ?? known?.href ?? null
		};
	}
	return null;
}

function safeDocsUrl(url) {
	const value = text(url);
	return /^https:\/\//.test(value) || (value.startsWith('/') && !value.startsWith('//')) ? value : '';
}

/** One normalized check; unknown statuses read as `pending` (never as OK). */
export function normalizeCheck(check, index = 0) {
	const status = CHECK_STATUS[check?.status] ? check.status : 'pending';
	return {
		id: text(check?.id) || `check-${index}`,
		category: text(check?.category) || 'other',
		title: text(check?.title) || text(check?.id) || 'Unnamed check',
		status,
		reason: text(check?.reason),
		fix: normalizeFix(check?.fix),
		docsUrl: safeDocsUrl(check?.docs_url)
	};
}

/**
 * Normalize the `/setup/status` payload.
 * `complete` is true only when the server says so; a missing or malformed
 * payload is `null` (unknown), which hides the banner rather than nagging.
 */
export function normalizeSetupStatus(payload) {
	if (!payload || typeof payload !== 'object' || typeof payload.complete !== 'boolean') return null;
	const checks = Array.isArray(payload.checks) ? payload.checks.map(normalizeCheck) : [];
	const capabilities = CAPABILITIES.map(({ id, label }) => {
		const cap = payload.capabilities?.[id];
		return {
			id,
			label,
			state: text(cap?.state) || 'unknown',
			reason: text(cap?.reason)
		};
	});
	return {
		complete: payload.complete,
		checks,
		capabilities,
		actionCount: checks.filter((check) => check.status === 'action_needed' || check.status === 'blocked').length
	};
}

/** Checks grouped by category in the contract order; unknown categories go last. */
export function groupChecks(checks = []) {
	const known = new Set(SETUP_CATEGORIES.map((category) => category.id));
	const groups = SETUP_CATEGORIES.map((category) => ({
		...category,
		checks: checks.filter((check) => check.category === category.id)
	}));
	const others = checks.filter((check) => !known.has(check.category));
	if (others.length) groups.push({ id: 'other', label: 'Other', checks: others });
	return groups.filter((group) => group.checks.length > 0);
}

/** Capability state words the UI treats as active. */
export function isCapabilityActive(state) {
	return ['active', 'ok', 'enabled', 'ready'].includes(String(state).toLowerCase());
}

/** Normalized repository suggestions (F2). */
export function normalizeSuggestions(payload) {
	const list = Array.isArray(payload?.suggestions) ? payload.suggestions : [];
	return list
		.filter((item) => item && typeof item.repo_url === 'string' && item.repo_url)
		.map((item) => ({
			repoUrl: item.repo_url,
			providerType: text(item.provider_type) || 'unknown',
			apiBaseUrl: text(item.api_base_url),
			inferredFrom: text(item.inferred_from),
			allowlisted: item.allowlisted === true
		}));
}

/** Pluralized call to action, e.g. "Allow these 3 repositories". */
export function allowLabel(count) {
	if (count === 1) return 'Allow this repository';
	return `Allow these ${count} repositories`;
}

/** Last setup status read by the layout or the setup page (null: unknown). */
export const setupStatus = writable(null);

/** sessionStorage key remembering that the setup banner was dismissed. */
export const SETUP_BANNER_DISMISSED_KEY = 'greenkube.setupBannerDismissed';

function sessionStore() {
	try {
		return typeof window !== 'undefined' ? window.sessionStorage ?? null : null;
	} catch {
		return null;
	}
}

function dismissedFlag() {
	let initial = false;
	try {
		initial = sessionStore()?.getItem(SETUP_BANNER_DISMISSED_KEY) === 'true';
	} catch {
		initial = false;
	}
	const store = writable(initial);
	function set(value) {
		const flag = value === true;
		store.set(flag);
		try {
			const storage = sessionStore();
			if (flag) storage?.setItem(SETUP_BANNER_DISMISSED_KEY, 'true');
			else storage?.removeItem(SETUP_BANNER_DISMISSED_KEY);
		} catch {
			// Keep the in-memory value when storage is unavailable.
		}
	}
	return { subscribe: store.subscribe, set, update: (fn) => set(fn(get(store))) };
}

/** Whether the setup banner was dismissed in this browser tab session. */
export const setupBannerDismissed = dismissedFlag();
