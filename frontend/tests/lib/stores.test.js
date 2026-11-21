/**
 * Tests for the Svelte store definitions.
 *
 * Verifies default values and write behaviour for all stores.
 */
import { afterEach, describe, it, expect, vi } from 'vitest';
import { get } from 'svelte/store';
import { createMemoryStorage, installBlockedStorage, installStorage } from '../helpers/storage.js';
import {
	selectedNamespace,
	selectedTimeRange,
	timeRanges,
	sidebarCollapsed,
	servicesHealth,
	healthPopupDismissed
} from '#lib/stores.js';


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


describe('healthPopupDismissed session persistence (K9)', () => {
	let restore = [];

	afterEach(() => {
		restore.reverse().forEach((undo) => undo());
		restore = [];
		vi.resetModules();
	});

	it('remembers the dismissal across reloads of the same tab', async () => {
		const storage = createMemoryStorage();
		restore.push(installStorage('sessionStorage', storage));
		vi.resetModules();
		const first = await import('#lib/stores.js');

		first.healthPopupDismissed.set(true);
		expect(storage.getItem(first.HEALTH_POPUP_DISMISSED_KEY)).toBe('true');

		vi.resetModules();
		const reloaded = await import('#lib/stores.js');
		expect(get(reloaded.healthPopupDismissed)).toBe(true);

		reloaded.healthPopupDismissed.update(() => false);
		expect(storage.getItem(reloaded.HEALTH_POPUP_DISMISSED_KEY)).toBeNull();
	});

	it('falls back to memory when sessionStorage is unavailable', async () => {
		restore.push(installBlockedStorage('sessionStorage'));
		vi.resetModules();
		const stores = await import('#lib/stores.js');

		expect(get(stores.healthPopupDismissed)).toBe(false);
		expect(() => stores.healthPopupDismissed.set(true)).not.toThrow();
		expect(get(stores.healthPopupDismissed)).toBe(true);
	});
});
