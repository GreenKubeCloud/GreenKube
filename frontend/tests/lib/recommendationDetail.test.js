import { describe, expect, it } from 'vitest';
import V2_DETAIL from '../fixtures/api/recommendation_v2_detail.json';
import {
	consensusOf,
	revisionsOf,
	uncountedReason,
	eventLabel,
	evidenceOf,
	formatConsensusValue,
	formatResource,
	impactOf,
	measuredOf,
	recommendationStatus,
	requestRows,
	safeHttpUrl,
	valueOf
} from '$lib/recommendationDetail.js';

describe('impactOf', () => {
	it('maps the Impact Model projection with its non-zero components', () => {
		const impact = impactOf({
			impact: {
				model_version: '1.0',
				kind: 'container_resources',
				currency: 'EUR',
				allocated_cost_annual: 120,
				expected_billable_cost_annual: 60,
				realization_factor: 0.5,
				co2e_allocated_annual_g: 9000,
				co2e_expected_annual_g: 4500,
				co2e_measurable_expected_annual_g: 3000,
				co2e_modeled: true,
				cost_breakdown_annual: { cpu: 80, ram: 40, gpu: 0, storage: 0 },
				co2e_components_annual_g: { operational_energy: 6000, idle_share: 0, embodied: 3000 },
				assumptions: { pricing: 'on_demand', nested: { ignored: true } }
			}
		});

		expect(impact).toMatchObject({
			modelVersion: '1.0',
			currency: 'EUR',
			allocatedCostAnnual: 120,
			expectedCostAnnual: 60,
			realizationFactor: 0.5,
			co2eExpectedAnnualG: 4500,
			co2eMeasurableAnnualG: 3000,
			co2eModeled: true
		});
		expect(impact.costComponents.map((row) => row.label)).toEqual(['CPU', 'Memory']);
		expect(impact.co2eComponents.map((row) => [row.label, row.value])).toEqual([
			['Operational energy', 6000],
			['Embodied (hardware)', 3000]
		]);
		expect(impact.assumptions).toEqual([['pricing', 'on_demand']]);
	});

	it('falls back to the raw CO2e breakdown and is null without a projection', () => {
		const impact = impactOf({ impact: { co2e_breakdown_annual_g: { dynamic_operational: 10, total: 10 } } });
		expect(impact.co2eComponents).toEqual([{ key: 'dynamic_operational', label: 'Dynamic CPU', value: 10 }]);
		expect(impact.currency).toBe('USD');
		expect(impactOf({})).toBeNull();
		expect(impactOf({ impact: 'nope' })).toBeNull();
	});

	it('reports CO2e as not modeled only when the server says so', () => {
		expect(impactOf({ impact: { co2e_modeled: false } }).co2eModeled).toBe(false);
		expect(impactOf({ impact: {} }).co2eModeled).toBe(true);
	});
});

describe('valueOf', () => {
	it('reads the expected-value block', () => {
		expect(valueOf({ value: { ev: '42.5', p_success: 0.8, lane: 'reliability', currency: 'GBP' } })).toMatchObject({
			ev: 42.5,
			pSuccess: 0.8,
			lane: 'reliability',
			currency: 'GBP'
		});
		expect(valueOf({})).toBeNull();
	});
});

describe('evidenceOf', () => {
	it('maps the review evidence and its usage percentiles', () => {
		const evidence = evidenceOf({
			evidence: {
				observation_window_start: '2026-01-01T00:00:00Z',
				sample_count: 1200,
				coverage_ratio: 0.95,
				cpu_usage: { avg: 100, p95: 180, max: 250, sample_count: 1200 },
				oom_events: 0,
				risk_factors: ['bursty'],
				rollback_conditions: [{ metric: 'oom', comparator: 'gt', threshold: 0 }],
				proposed_diff: '- cpu: 500m\n+ cpu: 250m',
				savings_method: 'request_delta'
			}
		});
		expect(evidence).toMatchObject({ samples: 1200, coverage: 0.95, oomEvents: 0, savingsMethod: 'request_delta' });
		expect(evidence.cpu).toMatchObject({ avg: 100, p95: 180, p50: null, max: 250, samples: 1200 });
		expect(evidence.memory).toBeNull();
		expect(evidence.riskFactors).toEqual(['bursty']);
		expect(evidence.diff).toContain('+ cpu: 250m');
		expect(evidenceOf({ evidence: null })).toBeNull();
	});
});

describe('the generated GET /api/v2/recommendations/{id} response', () => {
	it('maps the consensus, provenance and revisions the server returns', () => {
		const consensus = consensusOf({ id: 12 }, V2_DETAIL);
		expect(consensus.contested).toBe(true);
		expect(consensus.agreement).toBe(0.4);
		expect(consensus.reasons).toHaveLength(2);
		expect(consensus.sourceIds).toEqual(['goldilocks', 'vpa', 'krr', 'push:ci']);
		expect(consensus.revision).toBe(3);
		expect(consensus.votes.map((item) => [item.sourceId, item.counted])).toEqual([
			['goldilocks', true],
			['vpa', false],
			['krr', true],
			['push:ci', true]
		]);
		expect(uncountedReason(consensus.votes[1], consensus.votes)).toBe('Same engine as another source, not counted');

		const revisions = revisionsOf(V2_DETAIL);
		expect(revisions.revision).toBe(3);
		expect(revisions.queuedRevision).toBe(4);
		expect(revisions.queued.cpuRequest).toBe(260);
		expect(revisions.history.map((item) => [item.revision, item.state])).toEqual([
			[4, 'queued'],
			[2, 'superseded']
		]);
		expect(revisionsOf(null)).toBeNull();
	});

	it('explains a capped push vote', () => {
		const votes = [
			{ sourceId: 'push:ci', family: 'a', counted: true },
			{ sourceId: 'push:ci', family: 'b', counted: false }
		];
		expect(uncountedReason(votes[1], votes)).toBe('This push client already voted, not counted');
		expect(uncountedReason(votes[0], votes)).toBe('');
	});
});

describe('consensusOf', () => {
	it('is null for a v1 record so the section stays hidden', () => {
		expect(consensusOf({ id: 1, source: 'greenkube' })).toBeNull();
	});

	it('maps a Recommendation v2 consensus block', () => {
		const consensus = consensusOf({
			revision: 3,
			consensus: {
				policy: 'safest',
				resource: 'cpu_request_m',
				value: 300,
				chosen_from: ['vpa', 'greenkube'],
				agreement: 0.75,
				contested: false,
				reasons: ['directions disagree', 7],
				independent_votes: 2,
				spread: { cpu_request_m: { vpa: 250, greenkube: 300 }, bogus: 'x' },
				votes: [
					{ source_id: 'vpa', family: 'vpa', value: 250, direction: 'decrease', counted: true, supports_chosen: false },
					{ source_id: 'goldilocks', family: 'vpa', value: 250, counted: false },
					{ value: 1 }
				]
			}
		});
		expect(consensus).toMatchObject({
			policy: 'safest',
			resource: 'cpu_request_m',
			value: 300,
			chosenFrom: ['vpa', 'greenkube'],
			agreement: 0.75,
			independentVotes: 2,
			reasons: ['directions disagree'],
			revision: 3
		});
		expect(consensus.spread).toEqual([
			{
				resource: 'cpu_request_m',
				values: [
					{ source: 'vpa', value: 250, display: '250m' },
					{ source: 'greenkube', value: 300, display: '300m' }
				]
			}
		]);
		expect(consensus.votes).toEqual([
			{ sourceId: 'vpa', family: 'vpa', value: 250, direction: 'decrease', counted: true, supportsChosen: false },
			{ sourceId: 'goldilocks', family: 'vpa', value: 250, direction: null, counted: false, supportsChosen: false }
		]);
	});

	it('formats consensus values in their resource unit', () => {
		expect(formatConsensusValue('memory_request_bytes', 512 * 1024 * 1024)).toBe('512Mi');
		expect(formatConsensusValue('cpu_request_m', 250)).toBe('250m');
		expect(formatConsensusValue('replicas', 3)).toBe('3');
		expect(formatConsensusValue('schedule', '0 8 * * 1-5')).toBe('0 8 * * 1-5');
		expect(formatConsensusValue('cpu_request_m', null)).toBe('—');
	});
});

describe('measuredOf', () => {
	it('reads measured outcomes and is null when nothing was measured', () => {
		expect(measuredOf({ measured_cost_saved: 30, savings_realized: true, verification_status: 'verified' })).toMatchObject({
			costAnnual: 30,
			co2eAnnualG: null,
			realized: true,
			status: 'verified'
		});
		expect(measuredOf({ potential_savings_cost: 99 })).toBeNull();
	});
});

describe('helpers', () => {
	it('keeps only http(s) links', () => {
		expect(safeHttpUrl('https://github.com/acme/app/pull/4')).toBe('https://github.com/acme/app/pull/4');
		expect(safeHttpUrl('javascript:alert(1)')).toBeNull();
		expect(safeHttpUrl('not a url')).toBeNull();
		expect(safeHttpUrl(null)).toBeNull();
	});

	it('names statuses and events in words', () => {
		expect(recommendationStatus('pr_open')).toEqual({ label: 'Pull request open', tone: 'blue' });
		expect(recommendationStatus('brand_new')).toEqual({ label: 'brand new', tone: 'dark' });
		expect(eventLabel('pr_merged')).toBe('Pr merged');
		expect(eventLabel('')).toBe('Event');
	});

	it('formats requests as current → proposed rows', () => {
		expect(formatResource('memory', 512 * 1024 * 1024)).toBe('512Mi');
		expect(formatResource('memory', 1.5 * 1024 * 1024 * 1024)).toBe('1.50Gi');
		expect(formatResource('cpu', 250)).toBe('250m');
		expect(formatResource('cpu', null)).toBe('—');
		expect(
			requestRows({
				current_cpu_request_millicores: 500,
				recommended_cpu_request_millicores: 250,
				current_memory_request_bytes: 1024 * 1024 * 1024
			})
		).toEqual([
			{ resource: 'CPU request', current: '500m', proposed: '250m' },
			{ resource: 'Memory request', current: '1Gi', proposed: '—' }
		]);
		expect(requestRows({})).toEqual([]);
	});
});
