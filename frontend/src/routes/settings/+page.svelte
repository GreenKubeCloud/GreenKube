<script>
	import { onMount } from 'svelte';
	import { getConfig, getVersion, getHealth, getServicesHealth } from '$lib/api.js';
	import { servicesHealth, currentUser, hasScope } from '$lib/stores.js';
	import { allHelmSnippets, HELM_UPGRADE_COMMAND } from '$lib/configSnippets.js';
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

	// Data sources are configuration as code (A13): shown read-only with Helm snippets.
	const snippets = allHelmSnippets();

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
	$: activeServiceList = serviceList.filter(s => !s.inactive);
	$: inactiveServiceList = serviceList.filter(s => s.inactive);
	$: healthyCount = activeServiceList.filter(s => s.status === 'healthy').length;
	$: issueCount = activeServiceList.filter(s => s.status !== 'healthy').length;
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
							<HealthBadge status={svc.inactive ? 'inactive' : svc.status} compact />
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

		<!-- Data sources: configuration as code (A13) -->
		<Card title="Configure data sources" icon="🛠️">
			<p class="text-xs text-dark-500 mb-4">
				Data-source URLs and tokens are configuration as code: set them in your Helm values (or in
				the Secret named by <code>secrets.existingSecret</code>) and apply them with
				<code>helm upgrade</code> or your GitOps tool. Every GreenKube process then uses the same
				values, and Git stays the source of truth. They cannot be edited here.
			</p>
			<div class="grid grid-cols-1 lg:grid-cols-2 gap-4" data-testid="helm-snippets">
				{#each snippets as snippet (snippet.name)}
					<div class="p-4 rounded-xl bg-dark-800/50 border border-dark-700/30 space-y-2">
						<div class="flex items-center justify-between">
							<span class="text-sm font-medium text-dark-200">{snippet.label}</span>
							{#if services[snippet.name]}
								<HealthBadge
									status={services[snippet.name].inactive ? 'inactive' : services[snippet.name].status}
									compact
								/>
							{/if}
						</div>
						<pre class="text-[11px] leading-relaxed bg-dark-950 border border-dark-700/50 rounded-lg p-2 overflow-x-auto text-dark-200"><code>{snippet.helm}</code></pre>
						<p class="text-[11px] text-dark-500">{snippet.note}</p>
					</div>
				{/each}
			</div>
			<p class="text-xs text-dark-500 mt-4">Apply a values file:</p>
			<pre class="text-[11px] bg-dark-950 border border-dark-700/50 rounded-lg p-2 overflow-x-auto text-dark-200 mt-1"><code>{HELM_UPGRADE_COMMAND}</code></pre>
		</Card>

		<!-- Account and access (K11) -->
		{#if $currentUser && $currentUser.kind !== 'anonymous'}
			<Card title="Account and access" icon="🔐">
				<div class="flex flex-wrap gap-3">
					{#if $currentUser.kind === 'user'}
						<a href="/settings/account" class="btn-secondary text-sm">Account, password and sessions</a>
					{:else if $currentUser.kind === 'apikey'}
						<a href="/settings/account" class="btn-secondary text-sm">Account and sign-out</a>
					{/if}
					{#if hasScope($currentUser, 'admin')}
						<a href="/settings/api-keys" class="btn-secondary text-sm">API keys</a>
					{/if}
				</div>
			</Card>
		{/if}

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
					<code class="text-xs text-dark-300 bg-dark-800 px-2 py-1 rounded-sm">/api/v1</code>
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
