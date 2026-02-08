<script>
	import { onMount } from 'svelte';
	import { selectedNamespace, selectedTimeRange } from '$lib/stores.js';
	import { getMetricsSummary, getTimeseries, getMetrics, getRecommendations } from '$lib/api.js';
	import { formatCO2, formatCost, formatEnergy, formatNumber } from '$lib/utils/format.js';
	import { buildTimeseriesOption, buildMultiSeriesOption, buildNamespaceDonutOption, buildTopPodsOption } from '$lib/charts.js';
	import StatCard from '$lib/components/StatCard.svelte';
	import DataState from '$lib/components/DataState.svelte';
	import Filters from '$lib/components/Filters.svelte';
	import Chart from '$lib/components/Chart.svelte';
	import Card from '$lib/components/Card.svelte';

	let summary = null;
	let timeseries = [];
	let metrics = [];
	let recommendations = [];
	let loading = true;
	let error = null;

	$: params = { namespace: $selectedNamespace, last: $selectedTimeRange };
	$: if (params) loadData();

	async function loadData() {
		loading = true;
		error = null;
		try {
			const ns = $selectedNamespace || undefined;
			const last = $selectedTimeRange;
			const [s, ts, m, r] = await Promise.all([
				getMetricsSummary({ namespace: ns, last }),
				getTimeseries({ namespace: ns, last, granularity: last === '1h' ? 'hour' : 'day' }),
				getMetrics({ namespace: ns, last }),
				getRecommendations({ namespace: ns })
			]);
			summary = s;
			timeseries = ts;
			metrics = m;
			recommendations = r;
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	$: co2Option = timeseries.length
		? buildTimeseriesOption(timeseries, { valueKey: 'co2e_grams', label: 'CO₂ Emissions', unit: 'g' })
		: null;

	$: multiOption = timeseries.length ? buildMultiSeriesOption(timeseries) : null;

	$: nsDonutOption = metrics.length
		? buildNamespaceDonutOption(metrics, { label: 'CO₂ by Namespace' })
		: null;

	$: topPodsOption = metrics.length
		? buildTopPodsOption(metrics, { label: 'Top Pods by CO₂', topN: 8 })
		: null;

	onMount(() => loadData());
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Dashboard</h1>
			<p class="text-sm text-dark-500 mt-1">
				Environmental impact overview of your Kubernetes cluster
			</p>
		</div>
		<Filters />
	</div>

	<DataState {loading} {error} empty={!summary}>
		<!-- KPI Row -->
		<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
			<StatCard
				label="Total CO₂"
				value={formatCO2(summary?.total_co2e_grams ?? 0)}
				icon="🌿"
				color="green"
			/>
			<StatCard
				label="Total Cost"
				value={formatCost(summary?.total_cost ?? 0)}
				icon="💰"
				color="blue"
			/>
			<StatCard
				label="Total Energy"
				value={formatEnergy(summary?.total_joules ?? 0)}
				icon="⚡"
				color="yellow"
			/>
			<StatCard
				label="Active Pods"
				value={formatNumber(summary?.pod_count ?? 0)}
				icon="🔲"
				color="purple"
			/>
		</div>

		<!-- Additional Stats -->
		<div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
			<div class="card-compact text-center">
				<p class="stat-label">Embodied CO₂</p>
				<p class="stat-value text-lg">{formatCO2(summary?.total_embodied_co2e_grams ?? 0)}</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Namespaces</p>
				<p class="stat-value text-lg">{formatNumber(summary?.namespace_count ?? 0)}</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Avg CO₂/Pod</p>
				<p class="stat-value text-lg">
					{summary?.pod_count ? formatCO2((summary.total_co2e_grams ?? 0) / summary.pod_count) : '—'}
				</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Recommendations</p>
				<p class="stat-value text-lg {recommendations.length > 0 ? 'text-yellow-400' : ''}">
					{recommendations.length}
				</p>
			</div>
		</div>

		<!-- Charts Row 1 -->
		<div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
			<Card title="CO₂ Emissions Over Time" icon="📈">
				{#if co2Option}
					<Chart option={co2Option} height="320px" />
				{:else}
					<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
				{/if}
			</Card>

			<Card title="Multi-Metric Trend" icon="📊">
				{#if multiOption}
					<Chart option={multiOption} height="320px" />
				{:else}
					<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
				{/if}
			</Card>
		</div>

		<!-- Charts Row 2 -->
		<div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
			<Card title="CO₂ by Namespace" icon="🗂️">
				{#if nsDonutOption}
					<Chart option={nsDonutOption} height="320px" />
				{:else}
					<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
				{/if}
			</Card>

			<Card title="Top Pods by CO₂" icon="🏆">
				{#if topPodsOption}
					<Chart option={topPodsOption} height="320px" />
				{:else}
					<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
				{/if}
			</Card>
		</div>

		<!-- Recommendations preview -->
		{#if recommendations.length > 0}
			<Card title="Recent Recommendations" icon="💡">
				<div slot="actions">
					<a href="/recommendations" class="text-sm text-green-400 hover:text-green-300 transition-colors">
						View all →
					</a>
				</div>
				<div class="divide-y divide-dark-700/50">
					{#each recommendations.slice(0, 3) as rec}
						<div class="py-3 flex items-start gap-3">
							<span class="badge-{rec.type === 'ZOMBIE_POD' ? 'red' : 'yellow'} mt-0.5">
								{rec.type === 'ZOMBIE_POD' ? '💀' : '📐'}
							</span>
							<div class="flex-1 min-w-0">
								<p class="text-sm text-dark-200 font-medium truncate">{rec.pod_name}</p>
								<p class="text-xs text-dark-500 mt-0.5">{rec.reason}</p>
							</div>
							{#if rec.potential_savings_co2e_grams}
								<span class="text-xs text-green-400 whitespace-nowrap">
									-{formatCO2(rec.potential_savings_co2e_grams)}
								</span>
							{/if}
						</div>
					{/each}
				</div>
			</Card>
		{/if}
	</DataState>
</div>
