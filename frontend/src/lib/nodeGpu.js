/**
 * GPU power provenance of a node (NRG-13). `gpu_source` comes from the node's
 * latest idle row (`GET /nodes`): `dcgm` is measured; `gpu_tdp_estimate` and
 * `unmeasured` mean no DCGM exporter sample reached GreenKube.
 */

const BADGES = {
	gpu_tdp_estimate: {
		label: 'GPU power estimated',
		tone: 'yellow',
		title: 'No DCGM exporter sample: GPU power is estimated from the GPU model TDP. Deploy the DCGM exporter to measure it.'
	},
	unmeasured: {
		label: 'GPU power not measured',
		tone: 'red',
		title: 'No DCGM exporter sample and no TDP profile for this GPU model: GPU power is not counted. Deploy the DCGM exporter to measure it.'
	}
};

/** The badge `{ label, tone, title }` of a node whose GPU power is estimated or unmeasured, else `null`. */
export function gpuPowerBadge(node) {
	return BADGES[node?.gpu_source] ?? null;
}
