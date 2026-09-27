/**
 * Utility functions for the recommendation lifecycle event trail.
 */

/**
 * Collapses consecutive lifecycle events that share the same type and actor.
 *
 * Scheduled jobs re-emit the same event on every run (e.g. a repeated `failed`
 * verification), which is accurate but noisy. Grouping keeps the first and last
 * occurrence of each run plus the count, so a status change stays visible while
 * the intermediate repeats are folded away. The full trail is still returned by
 * the API.
 *
 * @param {Array<{event_type: string, actor?: string, created_at: string}>} events
 * @returns {Array<{event_type: string, actor: string, count: number, first: Object, last: Object}>}
 */
export function groupConsecutiveEvents(events = []) {
	const groups = [];
	for (const event of events) {
		const previous = groups[groups.length - 1];
		if (previous && previous.event_type === event.event_type && previous.actor === event.actor) {
			previous.count += 1;
			previous.last = event;
		} else {
			groups.push({
				event_type: event.event_type,
				actor: event.actor,
				count: 1,
				first: event,
				last: event
			});
		}
	}
	return groups;
}
