<script>
	import { onMount } from 'svelte';
	import { selectedNamespace, selectedTimeRange } from '$lib/stores.js';
	import { getMetrics, getTimeseries } from '$lib/api.js';
	import { formatCO2, formatCost, formatEnergy, formatDate, formatCPU, formatBytes } from '$lib/utils/format.js';
	import { buildTimeseriesOption } from '$lib/charts.js';
	import DataState from '$lib/components/DataState.svelte';
	import Filters from '$lib/components/Filters.svelte';
	import Card from '$lib/components/Card.svelte';
	import Chart from '$lib/components/Chart.svelte';

	let metrics = [];
	let timeseries = [];
	let loading = true;
	let error = null;

	let sortKey = 'co2e_grams';
	let sortDir = 'desc';
	let searchQuery = '';
	let currentPage = 1;
	const pageSize = 15;

	$: params = { namespace: $selectedNamespace, last: $selectedTimeRange };
	$: if (params) loadData();

	async function loadData() {
		loading = true;
		error = null;
		try {
			const ns = $selectedNamespace || undefined;
			const last = $selectedTimeRange;
			const [m, ts] = await Promise.all([
				getMetrics({ namespace: ns, last }),
				getTimeseries({ namespace: ns, last, granularity: last === '1h' ? 'hour' : 'day' })
			]);
			metrics = m;
			timeseries = ts;
			currentPage = 1;
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	function sort(key) {
		if (sortKey === key) {
			sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		} else {
			sortKey = key;
			sortDir = 'desc';
		}
	}

	$: filtered = metrics.filter(m => {
		if (!searchQuery) return true;
		const q = searchQuery.toLowerCase();
		return m.pod_name?.toLowerCase().includes(q) || m.namespace?.toLowerCase().includes(q);
	});

	$: sorted = [...filtered].sort((a, b) => {
		const av = a[sortKey] ?? 0;
		const bv = b[sortKey] ?? 0;
		if (typeof av === 'string') {
			return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
		}
		return sortDir === 'asc' ? av - bv : bv - av;
	});

	$: totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
	$: paged = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

	$: energyOption = timeseries.length
		? buildTimeseriesOption(timeseries, { valueKey: 'joules', label: 'Energy (J)', unit: 'J', color: '#eab308' })
		: null;

	$: costOption = timeseries.length
		? buildTimeseriesOption(timeseries, { valueKey: 'total_cost', label: 'Cost ($)', unit: '$', color: '#3b82f6' })
		: null;

	onMount(() => loadData());

	const columns = [
		{ key: 'pod_name', label: 'Pod' },
		{ key: 'namespace', label: 'Namespace' },
		{ key: 'co2e_grams', label: 'CO₂ (g)' },
		{ key: 'embodied_co2e_grams', label: 'Embodied CO₂ (g)' },
		{ key: 'total_cost', label: 'Cost ($)' },
		{ key: 'joules', label: 'Energy (J)' },
		{ key: 'cpu_request_millicores', label: 'CPU Req' },
		{ key: 'memory_request_bytes', label: 'Mem Req' },
		{ key: 'timestamp', label: 'Timestamp' }
	];
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Metrics</h1>
			<p class="text-sm text-dark-500 mt-1">
				Detailed per-pod environmental metrics
			</p>
		</div>
		<Filters />
	</div>

	<DataState {loading} {error} empty={!metrics.length} emptyMessage="No metrics found for the selected filters">
		<!-- Charts -->
		<div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
			<Card title="Energy Consumption" icon="⚡">
				{#if energyOption}
					<Chart option={energyOption} height="280px" />
				{:else}
					<div class="flex items-center justify-center h-72 text-dark-500">No data</div>
				{/if}
			</Card>
			<Card title="Cost Over Time" icon="💰">
				{#if costOption}
					<Chart option={costOption} height="280px" />
				{:else}
					<div class="flex items-center justify-center h-72 text-dark-500">No data</div>
				{/if}
			</Card>
		</div>

		<!-- Table -->
		<Card title="Pod Metrics" icon="📋">
			<div slot="actions">
				<span class="text-xs text-dark-500">{filtered.length} pods</span>
			</div>

			<!-- Search -->
			<div class="mb-4">
				<input
					type="text"
					bind:value={searchQuery}
					placeholder="Search pods or namespaces…"
					class="w-full sm:w-80 px-3 py-2 bg-dark-800 border border-dark-700 rounded-lg text-sm
					       text-dark-200 placeholder-dark-500 focus:outline-none focus:border-green-600
					       focus:ring-1 focus:ring-green-600/30 transition-colors"
				/>
			</div>

			<!-- Table -->
			<div class="overflow-x-auto -mx-5 px-5">
				<table class="w-full text-sm">
					<thead>
						<tr class="border-b border-dark-700/50">
							{#each columns as col}
								<th
									class="text-left py-3 px-3 text-xs font-medium text-dark-500 uppercase tracking-wider
									       cursor-pointer hover:text-dark-300 transition-colors select-none whitespace-nowrap"
									on:click={() => sort(col.key)}
								>
									<div class="flex items-center gap-1">
										{col.label}
										{#if sortKey === col.key}
											<span class="text-green-400">{sortDir === 'asc' ? '↑' : '↓'}</span>
										{/if}
									</div>
								</th>
							{/each}
						</tr>
					</thead>
					<tbody class="divide-y divide-dark-800">
						{#each paged as row}
							<tr class="hover:bg-dark-800/50 transition-colors">
								<td class="py-2.5 px-3 text-dark-200 font-mono text-xs truncate max-w-[200px]" title={row.pod_name}>
									{row.pod_name}
								</td>
								<td class="py-2.5 px-3">
									<span class="badge-green">{row.namespace}</span>
								</td>
								<td class="py-2.5 px-3 text-dark-300 font-mono text-xs">{formatCO2(row.co2e_grams)}</td>
								<td class="py-2.5 px-3 text-dark-300 font-mono text-xs">{formatCO2(row.embodied_co2e_grams)}</td>
								<td class="py-2.5 px-3 text-dark-300 font-mono text-xs">{formatCost(row.total_cost)}</td>
								<td class="py-2.5 px-3 text-dark-300 font-mono text-xs">{formatEnergy(row.joules)}</td>
								<td class="py-2.5 px-3 text-dark-300 font-mono text-xs">{formatCPU(row.cpu_request_millicores)}</td>
								<td class="py-2.5 px-3 text-dark-300 font-mono text-xs">{formatBytes(row.memory_request_bytes)}</td>
								<td class="py-2.5 px-3 text-dark-500 text-xs whitespace-nowrap">{formatDate(row.timestamp)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			<!-- Pagination -->
			{#if totalPages > 1}
				<div class="flex items-center justify-between mt-4 pt-4 border-t border-dark-700/50">
					<p class="text-xs text-dark-500">
						Page {currentPage} of {totalPages}
					</p>
					<div class="flex gap-1">
						<button
							class="px-3 py-1 text-xs rounded-lg bg-dark-800 text-dark-400
							       hover:bg-dark-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
							disabled={currentPage <= 1}
							on:click={() => currentPage--}
						>
							Previous
						</button>
						<button
							class="px-3 py-1 text-xs rounded-lg bg-dark-800 text-dark-400
							       hover:bg-dark-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
							disabled={currentPage >= totalPages}
							on:click={() => currentPage++}
						>
							Next
						</button>
					</div>
				</div>
			{/if}
		</Card>
	</DataState>
</div>
