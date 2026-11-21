/**
 * Tests for grouping GitOps discovery capabilities into one entry per controller family.
 */
import { describe, expect, it } from 'vitest';
import {
	CONTROLLER_FAMILIES,
	groupControllerFamilies,
	resourceKindState
} from '#lib/components/automation/gitopsControllers.js';

const ALL_KEYS = [
	'argocd',
	'flux_kustomization',
	'flux_helm_release',
	'flux_source_git_repository',
	'flux_source_oci_repository',
	'flux_source_helm_repository',
	'flux_source_bucket'
];

function discovery(overrides = {}, defaults = { available: true, reason: null }) {
	return Object.fromEntries(ALL_KEYS.map((key) => [key, overrides[key] ?? defaults]));
}

const missing = { available: false, reason: 'crd-not-found' };

describe('CONTROLLER_FAMILIES', () => {
	it('declares exactly one family per GitOps controller and covers every backend capability once', () => {
		expect(CONTROLLER_FAMILIES.map((family) => family.id)).toEqual(['argocd', 'flux']);
		const keys = CONTROLLER_FAMILIES.flatMap((family) =>
			family.groups.flatMap((group) => group.kinds.map((kind) => kind.key))
		);
		expect(keys.sort()).toEqual([...ALL_KEYS].sort());
	});
});

describe('resourceKindState', () => {
	it('maps availability to a safe status, label, and note', () => {
		expect(resourceKindState({ available: true, reason: null })).toEqual({
			status: 'available',
			statusLabel: 'Available',
			note: ''
		});
		expect(resourceKindState(missing)).toEqual({
			status: 'unavailable',
			statusLabel: 'Not installed',
			note: 'CRD not detected'
		});
	});

	it('never renders unrecognized server reasons', () => {
		expect(resourceKindState({ available: false, reason: 'Bearer secret detail' })).toEqual({
			status: 'unavailable',
			statusLabel: 'Not installed',
			note: 'Reason omitted'
		});
	});

	it('fails closed to unknown for missing or malformed records', () => {
		for (const value of [undefined, null, 'yes', [], {}, { available: 'true' }]) {
			expect(resourceKindState(value).status).toBe('unknown');
			expect(resourceKindState(value).statusLabel).toBe('Unknown');
		}
	});
});

describe('groupControllerFamilies', () => {
	it('returns one entry per family with detected status when every kind is available', () => {
		const families = groupControllerFamilies(discovery());

		expect(families).toHaveLength(2);
		const [argo, flux] = families;
		expect(argo).toMatchObject({
			id: 'argocd',
			label: 'Argo CD',
			status: 'detected',
			statusLabel: 'Detected',
			availableCount: 1,
			totalCount: 1
		});
		expect(flux).toMatchObject({
			id: 'flux',
			label: 'Flux',
			status: 'detected',
			statusLabel: 'Detected',
			availableCount: 6,
			totalCount: 6,
			summary: '6 of 6 resource kinds available',
			missingKinds: []
		});
		expect(flux.groups.map((group) => group.label)).toEqual(['Reconcilers', 'Sources']);
		expect(flux.groups[0].kinds.map((kind) => kind.label)).toEqual(['Kustomizations', 'HelmReleases']);
		expect(flux.groups[1].kinds.map((kind) => kind.label)).toEqual([
			'GitRepositories',
			'OCIRepositories',
			'HelmRepositories',
			'Buckets'
		]);
	});

	it('reports not detected when no kind of a family is installed', () => {
		const families = groupControllerFamilies(discovery({}, missing));

		expect(families.map((family) => family.status)).toEqual(['not_detected', 'not_detected']);
		expect(families[1].statusLabel).toBe('Not detected');
		expect(families[1].summary).toBe('0 of 6 resource kinds available');
	});

	it('reports a degraded family and names the missing kinds when only part of it is installed', () => {
		const families = groupControllerFamilies(
			discovery({
				flux_kustomization: missing,
				flux_source_oci_repository: { available: false, reason: 'unsafe provider detail' }
			})
		);
		const flux = families[1];

		expect(families[0].status).toBe('detected');
		expect(flux.status).toBe('degraded');
		expect(flux.statusLabel).toBe('Degraded');
		expect(flux.availableCount).toBe(4);
		expect(flux.summary).toBe('4 of 6 resource kinds available');
		expect(flux.missingKinds).toEqual(['Kustomizations', 'OCIRepositories']);
		const kinds = flux.groups.flatMap((group) => group.kinds);
		expect(kinds.find((kind) => kind.key === 'flux_kustomization')).toMatchObject({
			status: 'unavailable',
			note: 'CRD not detected'
		});
		expect(JSON.stringify(flux)).not.toContain('unsafe provider detail');
	});

	it('treats a partially unknown family with available kinds as degraded, not detected', () => {
		const record = discovery();
		delete record.flux_source_bucket;
		const flux = groupControllerFamilies(record)[1];

		expect(flux.status).toBe('degraded');
		expect(flux.missingKinds).toEqual(['Buckets']);
	});

	it('fails closed to unknown when the discovery payload is missing or malformed', () => {
		for (const value of [undefined, null, [], 'flux', { argocd: 'available' }]) {
			const families = groupControllerFamilies(value);
			expect(families).toHaveLength(2);
			expect(families.map((family) => family.status)).toEqual(['unknown', 'unknown']);
			expect(families[0].statusLabel).toBe('Unknown');
		}
	});

	it('keeps unknown, not not-detected, when some kinds are absent and the rest are unknown', () => {
		const flux = groupControllerFamilies({ flux_kustomization: missing })[1];

		expect(flux.status).toBe('unknown');
	});

	it('ignores capability keys that do not belong to a known family', () => {
		const families = groupControllerFamilies({ ...discovery(), flux_source_helm_chart: missing });

		expect(families[1].status).toBe('detected');
		expect(families[1].totalCount).toBe(6);
	});
});
