import { get, writable } from 'svelte/store';

/** Global filter state shared across pages */
export const selectedNamespace = writable('');
export const selectedTimeRange = writable('24h');

/** Available time ranges — these slugs align with the pre-computed summary windows */
export const timeRanges = [
	{ value: '1h', label: '1 hour' },
	{ value: '6h', label: '6 hours' },
	{ value: '24h', label: '24 hours' },
	{ value: '7d', label: '7 days' },
	{ value: '30d', label: '30 days' },
	{ value: '1y', label: '1 year' },
	{ value: 'ytd', label: 'Year to date' }
];

/** Sidebar state */
export const sidebarCollapsed = writable(false);

/**
 * The signed-in principal from GET /auth/me (F14): {kind, name, role, scopes,
 * must_change_password, ...}, or null while signed out. The session itself
 * lives in an HttpOnly cookie that scripts cannot read.
 */
export const currentUser = writable(null);

/** Whether the principal holds a scope (the admin scope implies every other). */
export function hasScope(user, scope) {
	const scopes = user?.scopes || [];
	return scopes.includes('admin') || scopes.includes(scope);
}

/**
 * True when the API runs `greenkube demo` (GET /health `demo`): synthetic data,
 * no cluster and pull requests disabled. Never true on a real install.
 */
export const demoMode = writable(false);

/** Services health state — populated on first load and refreshable */
export const servicesHealth = writable(null);

/** sessionStorage key remembering that the connectivity popup was dismissed. */
export const HEALTH_POPUP_DISMISSED_KEY = 'greenkube.healthPopupDismissed';

function sessionStore() {
	try {
		return typeof window !== 'undefined' ? window.sessionStorage ?? null : null;
	} catch {
		return null;
	}
}

/**
 * A boolean store mirrored to sessionStorage: it survives reloads of the tab
 * and resets when the tab closes. Storage failures fall back to memory only.
 */
function sessionFlag(key) {
	let initial = false;
	try {
		initial = sessionStore()?.getItem(key) === 'true';
	} catch {
		initial = false;
	}
	const store = writable(initial);
	function set(value) {
		const flag = value === true;
		store.set(flag);
		try {
			const storage = sessionStore();
			if (flag) storage?.setItem(key, 'true');
			else storage?.removeItem(key);
		} catch {
			// Keep the in-memory value when storage is unavailable.
		}
	}
	return {
		subscribe: store.subscribe,
		set,
		update: (updater) => set(updater(get(store)))
	};
}

/** Whether the connectivity popup has been dismissed in this browser tab session. */
export const healthPopupDismissed = sessionFlag(HEALTH_POPUP_DISMISSED_KEY);
