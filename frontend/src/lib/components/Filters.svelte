<script>
	import { selectedNamespace, selectedTimeRange, timeRanges } from '$lib/stores.js';
	import { getNamespaces } from '$lib/api.js';
	import { onMount } from 'svelte';

	let namespaces = [];

	onMount(async () => {
		try {
			namespaces = await getNamespaces();
		} catch {
			namespaces = [];
		}
	});
</script>

<div class="flex items-center gap-3 flex-wrap">
	<!-- Time Range -->
	<div class="flex items-center gap-2">
		<label for="time-range" class="text-xs text-dark-400 font-medium uppercase tracking-wider whitespace-nowrap">
			Period
		</label>
		<select
			id="time-range"
			bind:value={$selectedTimeRange}
			class="bg-dark-800 border border-dark-600/50 text-dark-200 text-sm rounded-lg px-3 py-1.5
			       focus:ring-2 focus:ring-green-500/50 focus:border-green-500 transition-colors"
		>
			{#each timeRanges as range}
				<option value={range.value}>{range.label}</option>
			{/each}
		</select>
	</div>

	<!-- Namespace -->
	<div class="flex items-center gap-2">
		<label for="namespace" class="text-xs text-dark-400 font-medium uppercase tracking-wider whitespace-nowrap">
			Namespace
		</label>
		<select
			id="namespace"
			bind:value={$selectedNamespace}
			class="bg-dark-800 border border-dark-600/50 text-dark-200 text-sm rounded-lg px-3 py-1.5
			       focus:ring-2 focus:ring-green-500/50 focus:border-green-500 transition-colors"
		>
			<option value="">All namespaces</option>
			{#each namespaces as ns}
				<option value={ns}>{ns}</option>
			{/each}
		</select>
	</div>
</div>
