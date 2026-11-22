/**
 * Mock for $app/state used by SvelteKit.
 */
export const page = {
	url: new URL('http://localhost:3000/'),
	params: {},
	route: { id: '/' },
	status: 200,
	state: {}
};

export const navigating = { current: null };
export const updated = { current: false, check: async () => false };
