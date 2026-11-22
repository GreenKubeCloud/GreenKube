/**
 * Deterministic Web Storage doubles for tests.
 *
 * Node 22+ exposes its own `localStorage`/`sessionStorage` globals, which can
 * shadow jsdom's and behave differently across Node versions. Tests that
 * depend on storage install these in-memory doubles on `window` instead.
 */

export function createMemoryStorage({ failing = false } = {}) {
	const data = new Map();
	const fail = () => {
		throw new DOMException('Storage is disabled.', 'SecurityError');
	};
	return {
		get length() {
			return data.size;
		},
		key: (index) => [...data.keys()][index] ?? null,
		getItem: (key) => (failing ? fail() : data.has(key) ? data.get(key) : null),
		setItem: (key, value) => (failing ? fail() : data.set(key, String(value))),
		removeItem: (key) => (failing ? fail() : data.delete(key)),
		clear: () => data.clear()
	};
}

/** Replace `window[name]` with `storage` and return a function restoring it. */
export function installStorage(name, storage) {
	const previous = Object.getOwnPropertyDescriptor(window, name);
	Object.defineProperty(window, name, { configurable: true, get: () => storage });
	return () => {
		if (previous) Object.defineProperty(window, name, previous);
		else delete window[name];
	};
}

/** Make reading `window[name]` itself throw, as browsers do when site data is blocked. */
export function installBlockedStorage(name) {
	const previous = Object.getOwnPropertyDescriptor(window, name);
	Object.defineProperty(window, name, {
		configurable: true,
		get: () => {
			throw new DOMException('Access is denied for this document.', 'SecurityError');
		}
	});
	return () => {
		if (previous) Object.defineProperty(window, name, previous);
		else delete window[name];
	};
}
