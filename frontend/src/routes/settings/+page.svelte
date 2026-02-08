<script>
	import { onMount } from 'svelte';
	import { getConfig, getVersion, getHealth } from '$lib/api.js';
	import DataState from '$lib/components/DataState.svelte';
	import Card from '$lib/components/Card.svelte';

	let config = null;
	let version = null;
	let health = null;
	let loading = true;
	let error = null;

	onMount(async () => {
		try {
			const [c, v, h] = await Promise.all([
				getConfig(),
				getVersion(),
				getHealth()
			]);
			config = c;
			version = v;
			health = h;
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	});

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
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div>
		<h1 class="text-2xl font-bold text-dark-100">Settings</h1>
		<p class="text-sm text-dark-500 mt-1">
			Current GreenKube configuration and system information
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
					<div class="w-2 h-2 rounded-full {health?.status === 'healthy' ? 'bg-green-500' : 'bg-red-500'}"></div>
					<p class="stat-value text-lg">{health?.status ?? '—'}</p>
				</div>
			</div>
			<div class="card-compact">
				<p class="stat-label">Database</p>
				<div class="flex items-center gap-2 mt-1">
					<div class="w-2 h-2 rounded-full {health?.database === 'connected' ? 'bg-green-500' : 'bg-yellow-500'}"></div>
					<p class="stat-value text-lg">{health?.database ?? '—'}</p>
				</div>
			</div>
		</div>

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
				<div class="flex items-center justify-between py-2">
					<span class="text-sm text-dark-400">Health Endpoint</span>
					<a href="/api/v1/health" target="_blank" class="text-xs text-green-400 hover:text-green-300 transition-colors">
						/api/v1/health ↗
					</a>
				</div>
			</div>
		</Card>
	</DataState>
</div>
