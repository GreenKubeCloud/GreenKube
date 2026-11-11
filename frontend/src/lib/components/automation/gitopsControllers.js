/**
 * Groups the per-CRD GitOps discovery capabilities returned by
 * GET /api/v1/gitops/status into one entry per controller family (Argo CD, Flux).
 *
 * The backend reports one `{available, reason}` record per resource kind; the
 * automation page shows one card per family with an overall status and a compact
 * list of its resource kinds. Grouping is client-side so the API contract stays
 * unchanged. Missing or malformed records fail closed to "unknown" and server
 * reason strings are never rendered verbatim.
 */

/**
 * @typedef {{ key: string, label: string }} ResourceKindDefinition
 * @typedef {{ label: string, kinds: ResourceKindDefinition[] }} ResourceKindGroupDefinition
 * @typedef {{ id: string, label: string, description: string, groups: ResourceKindGroupDefinition[] }} ControllerFamilyDefinition
 * @typedef {'available' | 'unavailable' | 'unknown'} ResourceKindStatus
 * @typedef {'detected' | 'not_detected' | 'degraded' | 'unknown'} ControllerFamilyStatus
 * @typedef {{ status: ResourceKindStatus, statusLabel: string, note: string }} ResourceKindState
 * @typedef {ResourceKindDefinition & ResourceKindState} ResourceKind
 * @typedef {{ label: string, kinds: ResourceKind[] }} ResourceKindGroup
 * @typedef {{
 *   id: string,
 *   label: string,
 *   description: string,
 *   status: ControllerFamilyStatus,
 *   statusLabel: string,
 *   availableCount: number,
 *   totalCount: number,
 *   summary: string,
 *   missingKinds: string[],
 *   groups: ResourceKindGroup[]
 * }} ControllerFamily
 */

/** @type {ControllerFamilyDefinition[]} */
export const CONTROLLER_FAMILIES = [
	{
		id: 'argocd',
		label: 'Argo CD',
		description: 'Applications reconcile workloads from Git.',
		groups: [{ label: 'Reconcilers', kinds: [{ key: 'argocd', label: 'Applications' }] }]
	},
	{
		id: 'flux',
		label: 'Flux',
		description: 'Kustomizations and HelmReleases reconcile workloads from Flux sources.',
		groups: [
			{
				label: 'Reconcilers',
				kinds: [
					{ key: 'flux_kustomization', label: 'Kustomizations' },
					{ key: 'flux_helm_release', label: 'HelmReleases' }
				]
			},
			{
				label: 'Sources',
				kinds: [
					{ key: 'flux_source_git_repository', label: 'GitRepositories' },
					{ key: 'flux_source_oci_repository', label: 'OCIRepositories' },
					{ key: 'flux_source_helm_repository', label: 'HelmRepositories' },
					{ key: 'flux_source_bucket', label: 'Buckets' }
				]
			}
		]
	}
];

const FAMILY_STATUS_LABELS = {
	detected: 'Detected',
	not_detected: 'Not detected',
	degraded: 'Degraded',
	unknown: 'Unknown'
};

/** @param {unknown} value */
function isRecord(value) {
	return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Project one backend capability record into a display-safe state.
 * @param {unknown} capability
 * @returns {ResourceKindState}
 */
export function resourceKindState(capability) {
	if (!isRecord(capability) || typeof capability.available !== 'boolean') {
		return { status: 'unknown', statusLabel: 'Unknown', note: '' };
	}
	if (capability.available) {
		return { status: 'available', statusLabel: 'Available', note: '' };
	}
	let note = '';
	if (capability.reason === 'crd-not-found') note = 'CRD not detected';
	else if (typeof capability.reason === 'string' && capability.reason) note = 'Reason omitted';
	return { status: 'unavailable', statusLabel: 'Not installed', note };
}

/**
 * @param {ResourceKind[]} kinds
 * @returns {ControllerFamilyStatus}
 */
function familyStatus(kinds) {
	const available = kinds.filter((kind) => kind.status === 'available').length;
	const unavailable = kinds.filter((kind) => kind.status === 'unavailable').length;
	if (available === kinds.length) return 'detected';
	if (unavailable === kinds.length) return 'not_detected';
	// Partly installed: the controller exists but some of its kinds cannot be read.
	if (available > 0) return 'degraded';
	// Nothing confirmed available and at least one kind unknown: do not claim absence.
	return 'unknown';
}

/**
 * Group the `discovery` object of the GitOps status response by controller family.
 * Always returns one entry per family, in display order.
 * @param {unknown} discovery
 * @returns {ControllerFamily[]}
 */
export function groupControllerFamilies(discovery) {
	const capabilities = isRecord(discovery) ? discovery : {};
	return CONTROLLER_FAMILIES.map((family) => {
		const groups = family.groups.map((group) => ({
			label: group.label,
			kinds: group.kinds.map((kind) => ({
				...kind,
				...resourceKindState(capabilities[kind.key])
			}))
		}));
		const kinds = groups.flatMap((group) => group.kinds);
		const status = familyStatus(kinds);
		const availableCount = kinds.filter((kind) => kind.status === 'available').length;
		const totalCount = kinds.length;
		return {
			id: family.id,
			label: family.label,
			description: family.description,
			status,
			statusLabel: FAMILY_STATUS_LABELS[status],
			availableCount,
			totalCount,
			summary: `${availableCount} of ${totalCount} resource kind${totalCount === 1 ? '' : 's'} available`,
			missingKinds:
				status === 'degraded'
					? kinds.filter((kind) => kind.status !== 'available').map((kind) => kind.label)
					: [],
			groups
		};
	});
}
