<script>
	import { createEventDispatcher } from 'svelte';
	import { updateServiceConfig } from '$lib/api.js';
	import HealthBadge from './HealthBadge.svelte';

	/** @type {Object|null} */
	export let services = null;
	/** @type {boolean} */
	export let visible = false;

	const dispatch = createEventDispatcher();

	let prometheusUrl = '';
	let opencostUrl = '';
	let electricityMapsToken = '';
	let saving = false;
	let saveError = '';

	$: issues = services ? Object.values(services).filter(
		s => s.status === 'unreachable' || s.status === 'unconfigured'
	) : [];

	$: hasIssues = issues.length > 0;

	async function saveConfig() {
		saving = true;
		saveError = '';
		try {
			const update = {};
			if (prometheusUrl) update.prometheus_url = prometheusUrl;
			if (opencostUrl) update.opencost_url = opencostUrl;
			if (electricityMapsToken) update.electricity_maps_token = electricityMapsToken;

			if (Object.keys(update).length === 0) {
				dispatch('dismiss');
				return;
			}

			const result = await updateServiceConfig(update);
			dispatch('updated', result);
			dispatch('dismiss');
		} catch (e) {
			saveError = e.message;
		} finally {
			saving = false;
		}
	}

	function dismiss() {
		dispatch('dismiss');
	}
</script>

{#if visible && hasIssues}
	<!-- Backdrop -->
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
		<!-- Modal -->
		<div class="bg-dark-900 border border-dark-700/50 rounded-2xl shadow-2xl w-full max-w-lg mx-4 overflow-hidden">
			<!-- Header -->
			<div class="flex items-center gap-3 px-6 py-4 border-b border-dark-700/50">
				<span class="text-2xl">⚠️</span>
				<div>
					<h2 class="text-lg font-bold text-dark-100">Service Connectivity Issues</h2>
					<p class="text-xs text-dark-500">
						{issues.length} service{issues.length > 1 ? 's' : ''} need{issues.length === 1 ? 's' : ''} attention
					</p>
				</div>
			</div>

			<!-- Issue List -->
			<div class="px-6 py-4 space-y-3 max-h-48 overflow-y-auto">
				{#each issues as svc}
					<div class="flex items-start gap-3 p-3 rounded-lg bg-dark-800/50">
						<HealthBadge status={svc.status} compact />
						<div class="flex-1 min-w-0">
							<p class="text-sm font-medium text-dark-200 capitalize">{svc.name.replace('_', ' ')}</p>
							<p class="text-xs text-dark-500 mt-0.5">{svc.message}</p>
						</div>
					</div>
				{/each}
			</div>

			<!-- Config Form -->
			<div class="px-6 py-4 border-t border-dark-700/50 space-y-3">
				<p class="text-xs text-dark-400">
					You can configure service URLs below. Changes apply to the current session only.
				</p>

				{#if services?.prometheus?.status === 'unconfigured' || services?.prometheus?.status === 'unreachable'}
					<div>
						<label for="prom-url" class="block text-xs font-medium text-dark-400 mb-1">Prometheus URL</label>
						<input
							id="prom-url"
							type="url"
							bind:value={prometheusUrl}
							placeholder="http://prometheus-server:9090"
							class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200
							       placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-green-500/50 focus:border-green-500/50"
						/>
					</div>
				{/if}

				{#if services?.opencost?.status === 'unconfigured' || services?.opencost?.status === 'unreachable'}
					<div>
						<label for="oc-url" class="block text-xs font-medium text-dark-400 mb-1">OpenCost URL</label>
						<input
							id="oc-url"
							type="url"
							bind:value={opencostUrl}
							placeholder="http://opencost:9003"
							class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200
							       placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-green-500/50 focus:border-green-500/50"
						/>
					</div>
				{/if}

				{#if services?.electricity_maps?.status === 'unconfigured'}
					<div>
						<label for="emaps-token" class="block text-xs font-medium text-dark-400 mb-1">
							Electricity Maps Token
							<a href="https://www.electricitymaps.com/" target="_blank" class="text-green-400 hover:text-green-300 ml-1">
								(Get free token ↗)
							</a>
						</label>
						<input
							id="emaps-token"
							type="text"
							bind:value={electricityMapsToken}
							placeholder="Your API token"
							class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200
							       placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-green-500/50 focus:border-green-500/50"
						/>
					</div>
				{/if}

				{#if saveError}
					<p class="text-xs text-red-400">{saveError}</p>
				{/if}
			</div>

			<!-- Actions -->
			<div class="flex items-center justify-end gap-3 px-6 py-4 border-t border-dark-700/50">
				<button
					on:click={dismiss}
					class="btn-secondary text-sm"
				>
					Dismiss
				</button>
				<button
					on:click={saveConfig}
					disabled={saving}
					class="btn-primary text-sm"
				>
					{saving ? 'Saving…' : 'Save & Retry'}
				</button>
			</div>
		</div>
	</div>
{/if}
