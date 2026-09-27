/**
 * Tests for the recommendation lifecycle trail utilities.
 */
import { describe, it, expect } from 'vitest';
import { groupConsecutiveEvents } from '$lib/utils/lifecycle.js';


function event(event_type, created_at, actor = 'system') {
	return { event_type, actor, created_at };
}

const T1 = '2026-09-27T10:00:00Z';
const T2 = '2026-09-27T10:05:00Z';
const T3 = '2026-09-27T10:10:00Z';
const T4 = '2026-09-27T10:15:00Z';


describe('groupConsecutiveEvents', () => {
	it('handles empty and undefined input', () => {
		expect(groupConsecutiveEvents([])).toEqual([]);
		expect(groupConsecutiveEvents()).toEqual([]);
	});

	it('keeps a single event as its own group', () => {
		const groups = groupConsecutiveEvents([event('applied', T1)]);

		expect(groups).toHaveLength(1);
		expect(groups[0].event_type).toBe('applied');
		expect(groups[0].count).toBe(1);
		expect(groups[0].first.created_at).toBe(T1);
		expect(groups[0].last.created_at).toBe(T1);
	});

	it('collapses consecutive events of the same type and actor', () => {
		const groups = groupConsecutiveEvents([
			event('failed', T1),
			event('failed', T2),
			event('failed', T3),
			event('failed', T4)
		]);

		expect(groups).toHaveLength(1);
		expect(groups[0].count).toBe(4);
		expect(groups[0].first.created_at).toBe(T1);
		expect(groups[0].last.created_at).toBe(T4);
	});

	it('splits runs when the event type changes', () => {
		const groups = groupConsecutiveEvents([
			event('verification_started', T1),
			event('failed', T2),
			event('failed', T3),
			event('verified', T4)
		]);

		expect(groups.map((g) => [g.event_type, g.count])).toEqual([
			['verification_started', 1],
			['failed', 2],
			['verified', 1]
		]);
	});

	it('does not merge identical events separated by another type', () => {
		const groups = groupConsecutiveEvents([
			event('failed', T1),
			event('verification_started', T2),
			event('failed', T3)
		]);

		expect(groups).toHaveLength(3);
		expect(groups[0].event_type).toBe('failed');
		expect(groups[1].event_type).toBe('verification_started');
		expect(groups[2].event_type).toBe('failed');
	});

	it('does not merge events from different actors', () => {
		const groups = groupConsecutiveEvents([
			event('failed', T1, 'system'),
			event('failed', T2, 'user'),
			event('failed', T3, 'system')
		]);

		expect(groups.map((g) => [g.actor, g.count])).toEqual([
			['system', 1],
			['user', 1],
			['system', 1]
		]);
	});

	it('does not mutate the input events', () => {
		const events = [event('failed', T1), event('failed', T2)];
		const snapshot = JSON.parse(JSON.stringify(events));

		groupConsecutiveEvents(events);

		expect(events).toEqual(snapshot);
	});
});
