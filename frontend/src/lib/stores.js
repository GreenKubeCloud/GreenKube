import { writable } from 'svelte/store';

/** Global filter state shared across pages */
export const selectedNamespace = writable('');
export const selectedTimeRange = writable('24h');

/** Available time ranges */
export const timeRanges = [
	{ value: '1h', label: '1 hour' },
	{ value: '6h', label: '6 hours' },
	{ value: '24h', label: '24 hours' },
	{ value: '7d', label: '7 days' },
	{ value: '30d', label: '30 days' }
];

/** Sidebar state */
export const sidebarCollapsed = writable(false);
