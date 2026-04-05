/**
 * Mock for $app/stores used by SvelteKit.
 */
import { writable, readable } from 'svelte/store';

export const page = readable({
	url: new URL('http://localhost:3000/'),
	params: {},
	route: { id: '/' },
	status: 200
});

export const navigating = readable(null);
export const updated = readable(false);
