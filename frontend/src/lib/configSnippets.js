/**
 * Helm values that configure each data source (roadmap A13).
 *
 * Data-source endpoints and tokens are configuration as code: the UI shows
 * them read-only and offers these snippets instead of editing them, so every
 * GreenKube process (API and controller) uses the same values and Git stays
 * the source of truth.
 */

const SNIPPETS = {
	prometheus: {
		label: 'Prometheus',
		helm: `config:
  prometheus:
    url: http://prometheus-operated.monitoring.svc:9090`,
		note: 'Leave the URL empty to keep auto-discovery. Credentials: secrets.prometheus.*.'
	},
	opencost: {
		label: 'OpenCost',
		helm: `config:
  opencost:
    url: http://opencost.opencost.svc:9003`,
		note: 'Leave the URL empty to keep auto-discovery.'
	},
	electricity_maps: {
		label: 'Electricity Maps',
		helm: `config:
  electricityProvider: electricity_maps
secrets:
  electricityMapsToken: <token>  # or key ELECTRICITY_MAPS_TOKEN in secrets.existingSecret`,
		note: 'Without a token, GreenKube uses the configured default grid intensity.'
	},
	wattnet: {
		label: 'Wattnet',
		helm: `config:
  electricityProvider: wattnet
secrets:
  wattnetEmail: <email>        # or keys WATTNET_EMAIL / WATTNET_PASSWORD
  wattnetPassword: <password>  # in secrets.existingSecret`,
		note: 'Free EU-funded service covering European zones.'
	},
	boavizta: {
		label: 'Boavizta',
		helm: `config:
  boavizta:
    url: https://api.boavizta.org`,
		note: 'Empty uses the public Boavizta API.'
	}
};

/** The Helm snippet for a data source, or null when it has no settings. */
export function helmSnippetFor(serviceName) {
	return SNIPPETS[serviceName] || null;
}

/** Every data source that has Helm settings, in display order. */
export function allHelmSnippets() {
	return Object.entries(SNIPPETS).map(([name, snippet]) => ({ name, ...snippet }));
}

/** The command that applies a values file to an installed release. */
export const HELM_UPGRADE_COMMAND =
	'helm upgrade greenkube greenkube/greenkube -n greenkube --reuse-values -f values-datasources.yaml';
