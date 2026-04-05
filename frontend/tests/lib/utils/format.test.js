/**
 * Tests for the format utility functions.
 *
 * Covers all formatters in src/lib/utils/format.js including edge cases
 * (null/undefined, zero, boundary values, large/small numbers).
 */
import { describe, it, expect } from 'vitest';
import {
	formatCO2,
	formatCost,
	formatEnergy,
	formatBytes,
	formatCPU,
	formatRelativeTime,
	formatDate,
	formatNumber
} from '$lib/utils/format.js';


// ---------------------------------------------------------------------------
// formatCO2
// ---------------------------------------------------------------------------
describe('formatCO2', () => {
	it('returns dash for null', () => {
		expect(formatCO2(null)).toBe('—');
	});

	it('returns dash for undefined', () => {
		expect(formatCO2(undefined)).toBe('—');
	});

	it('formats milligrams for sub-gram values', () => {
		expect(formatCO2(0.005)).toBe('5.0 mg');
		expect(formatCO2(0.1)).toBe('100.0 mg');
	});

	it('formats grams for values >= 1 and < 1000', () => {
		expect(formatCO2(1)).toBe('1.00 g');
		expect(formatCO2(42.567)).toBe('42.57 g');
		expect(formatCO2(999.99)).toBe('999.99 g');
		expect(formatCO2(999)).toBe('999.00 g');
	});

	it('formats kilograms for values >= 1000', () => {
		expect(formatCO2(1000)).toBe('1.00 kg');
		expect(formatCO2(2500)).toBe('2.50 kg');
		expect(formatCO2(100000)).toBe('100.00 kg');
	});

	it('handles zero', () => {
		// 0 grams → 0 mg
		expect(formatCO2(0)).toBe('0.0 mg');
	});
});


// ---------------------------------------------------------------------------
// formatCost
// ---------------------------------------------------------------------------
describe('formatCost', () => {
	it('returns dash for null', () => {
		expect(formatCost(null)).toBe('—');
	});

	it('returns dash for undefined', () => {
		expect(formatCost(undefined)).toBe('—');
	});

	it('formats with 2 decimals for values >= 1', () => {
		expect(formatCost(1)).toBe('$1.00');
		expect(formatCost(99.999)).toBe('$100.00');
		expect(formatCost(1234.56)).toBe('$1234.56');
	});

	it('formats with 3 decimals for 0.01 <= cost < 1', () => {
		expect(formatCost(0.01)).toBe('$0.010');
		expect(formatCost(0.123)).toBe('$0.123');
		expect(formatCost(0.5)).toBe('$0.500');
	});

	it('formats with 4 decimals for very small values', () => {
		expect(formatCost(0.001)).toBe('$0.0010');
		expect(formatCost(0.0001)).toBe('$0.0001');
	});

	it('handles zero', () => {
		expect(formatCost(0)).toBe('$0.0000');
	});
});


// ---------------------------------------------------------------------------
// formatEnergy
// ---------------------------------------------------------------------------
describe('formatEnergy', () => {
	it('returns dash for null', () => {
		expect(formatEnergy(null)).toBe('—');
	});

	it('returns dash for undefined', () => {
		expect(formatEnergy(undefined)).toBe('—');
	});

	it('formats Joules for small values', () => {
		expect(formatEnergy(0)).toBe('0 J');
		expect(formatEnergy(500)).toBe('500 J');
		expect(formatEnergy(999)).toBe('999 J');
	});

	it('formats kilojoules for values >= 1000', () => {
		expect(formatEnergy(1000)).toBe('1.0 kJ');
		expect(formatEnergy(1500)).toBe('1.5 kJ');
		expect(formatEnergy(999999)).toBe('1000.0 kJ');
	});

	it('formats kWh for large values', () => {
		expect(formatEnergy(3.6e6)).toBe('1.00 kWh');
		expect(formatEnergy(7.2e6)).toBe('2.00 kWh');
	});
});


// ---------------------------------------------------------------------------
// formatBytes
// ---------------------------------------------------------------------------
describe('formatBytes', () => {
	it('returns dash for null', () => {
		expect(formatBytes(null)).toBe('—');
	});

	it('returns dash for undefined', () => {
		expect(formatBytes(undefined)).toBe('—');
	});

	it('formats bytes', () => {
		expect(formatBytes(0)).toBe('0 B');
		expect(formatBytes(512)).toBe('512 B');
	});

	it('formats KB', () => {
		expect(formatBytes(1024)).toBe('1.0 KB');
		expect(formatBytes(1536)).toBe('1.5 KB');
	});

	it('formats MB', () => {
		expect(formatBytes(1048576)).toBe('1.0 MB');
	});

	it('formats GB', () => {
		expect(formatBytes(1073741824)).toBe('1.0 GB');
	});

	it('formats TB', () => {
		expect(formatBytes(1099511627776)).toBe('1.0 TB');
	});

	it('caps at TB for huge values', () => {
		const result = formatBytes(5 * 1099511627776);
		expect(result).toBe('5.0 TB');
	});
});


// ---------------------------------------------------------------------------
// formatCPU
// ---------------------------------------------------------------------------
describe('formatCPU', () => {
	it('returns dash for null', () => {
		expect(formatCPU(null)).toBe('—');
	});

	it('returns dash for undefined', () => {
		expect(formatCPU(undefined)).toBe('—');
	});

	it('formats millicores for < 1000', () => {
		expect(formatCPU(100)).toBe('100m');
		expect(formatCPU(500)).toBe('500m');
	});

	it('formats cores for >= 1000', () => {
		expect(formatCPU(1000)).toBe('1.0 cores');
		expect(formatCPU(2500)).toBe('2.5 cores');
		expect(formatCPU(16000)).toBe('16.0 cores');
	});
});


// ---------------------------------------------------------------------------
// formatRelativeTime
// ---------------------------------------------------------------------------
describe('formatRelativeTime', () => {
	it('returns dash for falsy input', () => {
		expect(formatRelativeTime(null)).toBe('—');
		expect(formatRelativeTime('')).toBe('—');
		expect(formatRelativeTime(undefined)).toBe('—');
	});

	it('returns "just now" for recent timestamps', () => {
		const now = new Date();
		expect(formatRelativeTime(now)).toBe('just now');
	});

	it('returns minutes ago', () => {
		const date = new Date(Date.now() - 5 * 60 * 1000);
		expect(formatRelativeTime(date)).toBe('5m ago');
	});

	it('returns hours ago', () => {
		const date = new Date(Date.now() - 3 * 3600 * 1000);
		expect(formatRelativeTime(date)).toBe('3h ago');
	});

	it('returns days ago', () => {
		const date = new Date(Date.now() - 2 * 86400 * 1000);
		expect(formatRelativeTime(date)).toBe('2d ago');
	});

	it('handles ISO string input', () => {
		const recent = new Date(Date.now() - 120 * 1000).toISOString();
		expect(formatRelativeTime(recent)).toBe('2m ago');
	});
});


// ---------------------------------------------------------------------------
// formatDate
// ---------------------------------------------------------------------------
describe('formatDate', () => {
	it('returns dash for falsy input', () => {
		expect(formatDate(null)).toBe('—');
		expect(formatDate('')).toBe('—');
		expect(formatDate(undefined)).toBe('—');
	});

	it('formats a Date object', () => {
		const result = formatDate(new Date('2026-03-15T14:30:00Z'));
		// Result depends on locale but should contain the month and a colon for time
		expect(result).toMatch(/Mar/);
		expect(result).toMatch(/:/);
	});

	it('formats an ISO string', () => {
		const result = formatDate('2026-12-25T08:00:00Z');
		expect(result).toMatch(/Dec/);
	});
});


// ---------------------------------------------------------------------------
// formatNumber
// ---------------------------------------------------------------------------
describe('formatNumber', () => {
	it('returns dash for null', () => {
		expect(formatNumber(null)).toBe('—');
	});

	it('returns dash for undefined', () => {
		expect(formatNumber(undefined)).toBe('—');
	});

	it('formats plain numbers < 1000', () => {
		expect(formatNumber(0)).toBe('0');
		expect(formatNumber(42)).toBe('42');
		expect(formatNumber(999)).toBe('999');
	});

	it('formats thousands as K', () => {
		expect(formatNumber(1000)).toBe('1.0K');
		expect(formatNumber(1500)).toBe('1.5K');
		expect(formatNumber(999999)).toBe('1000.0K');
	});

	it('formats millions as M', () => {
		expect(formatNumber(1e6)).toBe('1.0M');
		expect(formatNumber(2.5e6)).toBe('2.5M');
	});
});
