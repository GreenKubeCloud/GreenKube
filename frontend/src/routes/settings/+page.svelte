<script>
	import { onMount } from 'svelte';
	import { getConfig, getVersion, getHealth, getServicesHealth, updateServiceConfig } from '$lib/api.js';
	import { servicesHealth } from '$lib/stores.js';
	import DataState from '$lib/components/DataState.svelte';
	import Card from '$lib/components/Card.svelte';
	import HealthBadge from '$lib/components/HealthBadge.svelte';

	let config = null;
	let version = null;
	let health = null;
	let svcHealth = null;
	let loading = true;
	let error = null;
	let refreshing = false;

	// Editable service config
	let editPrometheusUrl = '';
	let editOpencostUrl = '';
	let editEmapsToken = '';
	let editBoaviztaUrl = '';
	let saving = false;
	let saveMessage = '';
	let saveError = '';

	onMount(async () => {
		try {
			const [c, v, h, sh] = await Promise.all([
				getConfig(),
				getVersion(),
				getHealth(),
				getServicesHealth()
			]);
			config = c;
			version = v;
			health = h;
			svcHealth = sh;
			servicesHealth.set(sh);
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	});

	async function refreshHealth() {
		refreshing = true;
		try {
			svcHealth = await getServicesHealth(true);
			servicesHealth.set(svcHealth);
		} catch (e) {
			// Silently fail
		} finally {
			refreshing = false;
		}
	}

	async function saveServiceConfig() {
		saving = true;
		saveMessage = '';
		saveError = '';
		try {
			const update = {};
			if (editPrometheusUrl) update.prometheus_url = editPrometheusUrl;
			if (editOpencostUrl) update.opencost_url = editOpencostUrl;
			if (editEmapsToken) update.electricity_maps_token = editEmapsToken;
			if (editBoaviztaUrl) update.boavizta_url = editBoaviztaUrl;

			if (Object.keys(update).length === 0) {
				saveError = 'No changes to save.';
				saving = false;
				return;
			}

			svcHealth = await updateServiceConfig(update);
			servicesHealth.set(svcHealth);

			// Clear inputs after successful save
			editPrometheusUrl = '';
			editOpencostUrl = '';
			editEmapsToken = '';
			editBoaviztaUrl = '';
			saveMessage = 'Configuration updated successfully. Health checks refreshed.';

			// Refresh config
			config = await getConfig();
		} catch (e) {
			saveError = e.message;
		} finally {
			saving = false;
		}
	}

	function flattenConfig(obj, prefix = '') {
		const result = [];
		for (const [key, value] of Object.entries(obj)) {
			const path = prefix ? `${prefix}.${key}` : key;
			if (value && typeof value === 'object' && !Array.isArray(value)) {
				result.push(...flattenConfig(value, path));
			} else {
				result.push({ key: path, value });
			}
		}
		return result;
	}

	function formatValue(val) {
		if (val === null || val === undefined) return '—';
		if (typeof val === 'boolean') return val ? 'true' : 'false';
		if (Array.isArray(val)) return val.join(', ') || '—';
		return String(val);
	}

	function isSensitive(key) {
		const lower = key.toLowerCase();
		return lower.includes('token') || lower.includes('password') || lower.includes('secret') || lower.includes('key');
	}

	$: configEntries = config ? flattenConfig(config) : [];
	$: services = svcHealth?.services || {};
	$: serviceList = Object.values(services);
	$: healthyCount = serviceList.filter(s => s.status === 'healthy').length;
	$: issueCount = serviceList.filter(s => s.status !== 'healthy').length;
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div>
		<h1 class="text-2xl font-bold text-dark-100">Settings</h1>
		<p class="text-sm text-dark-500 mt-1">
			Current GreenKube configuration, service health, and system information
		</p>
	</div>

	<DataState {loading} {error} empty={!config && !version}>
		<!-- System Info -->
		<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
			<div class="card-compact">
				<p class="stat-label">Version</p>
				<p class="stat-value text-lg font-mono">{version?.version ?? '—'}</p>
			</div>
			<div class="card-compact">
				<p class="stat-label">API Status</p>
				<div class="flex items-center gap-2 mt-1">
					<div class="w-2 h-2 rounded-full {health?.status === 'ok' ? 'bg-green-500' : 'bg-red-500'}"></div>
					<p class="stat-value text-lg">{health?.status ?? '—'}</p>
				</div>
			</div>
			<div class="card-compact">
				<p class="stat-label">Data Sources</p>
				<div class="flex items-center gap-3 mt-1">
					<span class="badge-green">{healthyCount} healthy</span>
					{#if issueCount > 0}
						<span class="badge-yellow">{issueCount} issue{issueCount > 1 ? 's' : ''}</span>
					{/if}
				</div>
			</div>
		</div>

		<!-- Service Health Overview -->
		<Card title="Service Health" icon="🔗">
			<div slot="actions">
				<button
					on:click={refreshHealth}
					disabled={refreshing}
					class="btn-secondary text-xs !py-1.5 !px-3"
				>
					{refreshing ? '⟳ Checking…' : '⟳ Refresh'}
				</button>
			</div>

			<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
				{#each serviceList as svc}
					<div class="p-4 rounded-xl bg-dark-800/50 border border-dark-700/30 space-y-2">
						<div class="flex items-center justify-between">
							<span class="text-sm font-medium text-dark-200 capitalize">
								{svc.name.replace('_', ' ')}
							</span>
							<HealthBadge status={svc.status} compact />
						</div>

						<p class="text-xs text-dark-500 line-clamp-2">{svc.message}</p>

						<div class="flex items-center justify-between text-[10px] text-dark-600">
							{#if svc.url}
								<span class="font-mono truncate max-w-[180px]" title={svc.url}>{svc.url}</span>
							{:else}
								<span>—</span>
							{/if}
							{#if svc.latency_ms != null}
								<span>{svc.latency_ms}ms</span>
							{/if}
						</div>

						{#if svc.discovered}
							<span class="badge bg-blue-900/40 text-blue-400 border border-blue-700/30 text-[10px]">
								Auto-discovered
							</span>
						{:else if svc.configured}
							<span class="badge bg-dark-800/50 text-dark-400 border border-dark-600/30 text-[10px]">
								Manually configured
							</span>
						{/if}
					</div>
				{/each}
			</div>
		</Card>

		<!-- Service Configuration -->
		<Card title="Configure Services" icon="🛠️">
			<p class="text-xs text-dark-500 mb-4">
				Override service URLs or tokens at runtime. These changes apply to the current session only.
				For permanent changes, update your Helm values or environment variables.
			</p>

			<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
				<div>
					<label for="edit-prom" class="block text-xs font-medium text-dark-400 mb-1">Prometheus URL</label>
					<input
						id="edit-prom"
						type="url"
						bind:value={editPrometheusUrl}
						placeholder={services.prometheus?.url || 'http://prometheus-server:9090'}
						class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200
						       placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-green-500/50"
					/>
				</div>
				<div>
					<label for="edit-oc" class="block text-xs font-medium text-dark-400 mb-1">OpenCost URL</label>
					<input
						id="edit-oc"
						type="url"
						bind:value={editOpencostUrl}
						placeholder={services.opencost?.url || 'http://opencost:9003'}
						class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200
						       placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-green-500/50"
					/>
				</div>
				<div>
					<label for="edit-emaps" class="block text-xs font-medium text-dark-400 mb-1">
						Electricity Maps Token
						<a href="https://www.electricitymaps.com/" target="_blank"
						   class="text-green-400 hover:text-green-300 ml-1 text-[10px]">(Get free ↗)</a>
					</label>
					<input
						id="edit-emaps"
						type="text"
						bind:value={editEmapsToken}
						placeholder="Your API token"
						class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200
						       placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-green-500/50"
					/>
				</div>
				<div>
					<label for="edit-boavizta" class="block text-xs font-medium text-dark-400 mb-1">Boavizta API URL</label>
					<input
						id="edit-boavizta"
						type="url"
						bind:value={editBoaviztaUrl}
						placeholder={services.boavizta?.url || 'https://api.boavizta.org'}
						class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200
						       placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-green-500/50"
					/>
				</div>
			</div>

			<div class="flex items-center gap-4 mt-4">
				<button
					on:click={saveServiceConfig}
					disabled={saving}
					class="btn-primary text-sm"
				>
					{saving ? 'Saving…' : 'Save & Check'}
				</button>
				{#if saveMessage}
					<span class="text-xs text-green-400">{saveMessage}</span>
				{/if}
				{#if saveError}
					<span class="text-xs text-red-400">{saveError}</span>
				{/if}
			</div>
		</Card>

		<!-- Configuration Table -->
		<Card title="Configuration" icon="⚙️">
			<div slot="actions">
				<span class="text-xs text-dark-500">{configEntries.length} settings</span>
			</div>

			<div class="overflow-x-auto -mx-5 px-5">
				<table class="w-full text-sm">
					<thead>
						<tr class="border-b border-dark-700/50">
							<th class="text-left py-3 px-3 text-xs font-medium text-dark-500 uppercase tracking-wider">
								Setting
							</th>
							<th class="text-left py-3 px-3 text-xs font-medium text-dark-500 uppercase tracking-wider">
								Value
							</th>
						</tr>
					</thead>
					<tbody class="divide-y divide-dark-800">
						{#each configEntries as entry}
							<tr class="hover:bg-dark-800/50 transition-colors">
								<td class="py-2.5 px-3 text-dark-300 font-mono text-xs">
									{entry.key}
								</td>
								<td class="py-2.5 px-3 font-mono text-xs">
									{#if isSensitive(entry.key)}
										<span class="text-dark-600">••••••••</span>
									{:else}
										<span class="{typeof entry.value === 'boolean'
											? entry.value ? 'text-green-400' : 'text-dark-500'
											: 'text-dark-200'}">
											{formatValue(entry.value)}
										</span>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</Card>

		<!-- API Info -->
		<Card title="API Information" icon="🔌">
			<div class="space-y-3">
				<div class="flex items-center justify-between py-2 border-b border-dark-800">
					<span class="text-sm text-dark-400">API Base URL</span>
					<code class="text-xs text-dark-300 bg-dark-800 px-2 py-1 rounded">/api/v1</code>
				</div>
				<div class="flex items-center justify-between py-2 border-b border-dark-800">
					<span class="text-sm text-dark-400">Documentation</span>
					<a href="/api/v1/docs" target="_blank" class="text-xs text-green-400 hover:text-green-300 transition-colors">
						OpenAPI Docs ↗
					</a>
				</div>
				<div class="flex items-center justify-between py-2 border-b border-dark-800">
					<span class="text-sm text-dark-400">Health Endpoint</span>
					<a href="/api/v1/health" target="_blank" class="text-xs text-green-400 hover:text-green-300 transition-colors">
						/api/v1/health ↗
					</a>
				</div>
				<div class="flex items-center justify-between py-2">
					<span class="text-sm text-dark-400">Services Health Endpoint</span>
					<a href="/api/v1/health/services" target="_blank" class="text-xs text-green-400 hover:text-green-300 transition-colors">
						/api/v1/health/services ↗
					</a>
				</div>
			</div>
		</Card>
	</DataState>
</div>
