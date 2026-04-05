/**
 * Tests for the Svelte store definitions.
 *
 * Verifies default values and write behaviour for all stores.
 */
import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import {
	selectedNamespace,
	selectedTimeRange,
	timeRanges,
	sidebarCollapsed,
	servicesHealth,
	healthPopupDismissed
} from '$lib/stores.js';


describe('selectedNamespace', () => {
	it('defaults to empty string (all namespaces)', () => {
		expect(get(selectedNamespace)).toBe('');
	});

	it('is writable', () => {
		selectedNamespace.set('production');
		expect(get(selectedNamespace)).toBe('production');
		// Reset
		selectedNamespace.set('');
	});
});


describe('selectedTimeRange', () => {
	it('defaults to 24h', () => {
		expect(get(selectedTimeRange)).toBe('24h');
	});

	it('is writable', () => {
		selectedTimeRange.set('7d');
		expect(get(selectedTimeRange)).toBe('7d');
		// Reset
		selectedTimeRange.set('24h');
	});
});


describe('timeRanges', () => {
	it('provides at least 3 range options', () => {
		expect(timeRanges.length).toBeGreaterThanOrEqual(3);
	});

	it('each range has value and label', () => {
		for (const range of timeRanges) {
			expect(range).toHaveProperty('value');
			expect(range).toHaveProperty('label');
			expect(typeof range.value).toBe('string');
			expect(typeof range.label).toBe('string');
		}
	});

	it('includes 24h as a range', () => {
		expect(timeRanges.some(r => r.value === '24h')).toBe(true);
	});
});


describe('sidebarCollapsed', () => {
	it('defaults to false', () => {
		expect(get(sidebarCollapsed)).toBe(false);
	});

	it('can be toggled', () => {
		sidebarCollapsed.set(true);
		expect(get(sidebarCollapsed)).toBe(true);
		sidebarCollapsed.set(false);
	});
});


describe('servicesHealth', () => {
	it('defaults to null', () => {
		expect(get(servicesHealth)).toBeNull();
	});

	it('can hold health data', () => {
		const data = { services: { prometheus: { status: 'healthy' } } };
		servicesHealth.set(data);
		expect(get(servicesHealth)).toEqual(data);
		servicesHealth.set(null);
	});
});


describe('healthPopupDismissed', () => {
	it('defaults to false', () => {
		expect(get(healthPopupDismissed)).toBe(false);
	});

	it('can be set to true', () => {
		healthPopupDismissed.set(true);
		expect(get(healthPopupDismissed)).toBe(true);
		healthPopupDismissed.set(false);
	});
});
