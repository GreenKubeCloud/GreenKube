import { describe, expect, it } from 'vitest';
import { runTone, sortSources, sourceStatus, trackRecordFor } from '$lib/sources.js';

describe('sources', () => {
	it('labels every status with text and an icon', () => {
		expect(sourceStatus('failing')).toEqual({ label: 'Failing', tone: 'red', icon: '✕' });
		expect(sourceStatus('pending').label).toBe('Not run yet');
		expect(sourceStatus('new_status')).toEqual({ label: 'new_status', tone: 'dark', icon: '?' });
	});

	it('tones run statuses, skipped runs included', () => {
		expect(runTone('succeeded')).toBe('green');
		expect(runTone('skipped')).toBe('yellow');
		expect(runTone('failed')).toBe('red');
		expect(runTone('other')).toBe('dark');
	});

	it('lists problem sources first and keeps the server order otherwise', () => {
		const sorted = sortSources([
			{ id: 'a', status: 'healthy' },
			{ id: 'b', status: 'degraded' },
			{ id: 'c', status: 'pending' },
			{ id: 'd', status: 'failing' },
			{ id: 'e', status: 'misconfigured' }
		]);
		expect(sorted.map((source) => source.id)).toEqual(['d', 'e', 'b', 'a', 'c']);
	});

	it('matches track-record entries by legacy family or id', () => {
		const entries = [
			{ source_family: 'greenkube', capability: 'rightsizing', summary: '8/10 realized', mean: 0.75, decided: 10 },
			{ source_family: 'vpa', capability: 'rightsizing', mean: 0.5, decided: 2 },
			{ source_family: 'krr', capability: 'all', decided: 0 }
		];
		expect(trackRecordFor({ id: 'builtin.rightsizing', legacy_source: 'greenkube' }, entries)).toEqual([
			{ capability: 'rightsizing', summary: '8/10 realized', mean: 0.75, decided: 10 }
		]);
		expect(trackRecordFor({ id: 'vpa' }, entries)).toEqual([{ capability: 'rightsizing', summary: '', mean: 0.5, decided: 2 }]);
		expect(trackRecordFor({ id: 'kubecost' }, entries)).toEqual([]);
		expect(trackRecordFor({ id: 'vpa' }, null)).toEqual([]);
	});
});
