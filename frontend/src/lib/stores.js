import { writable } from 'svelte/store';

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

/** Services health state — populated on first load and refreshable */
export const servicesHealth = writable(null);

/** Whether the initial health check popup has been dismissed */
export const healthPopupDismissed = writable(false);
